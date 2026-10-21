# -*- coding: utf-8 -*-
"""
conversor.py - Conversion de planos PDF a DWG / DXF con QCAD.

QCAD Professional hace la conversion: lee cada lamina con su importador de PDF
(el mismo de su herramienta pdf2dwg) y la guarda como DWG o DXF. Este modulo:

  * genera un script de QCAD que convierte muchas laminas por cada arranque
    (la version de prueba de QCAD espera 15 s cada vez que se abre),
  * lanza varios QCAD en paralelo, sigue su avance lamina por lamina y, si un
    QCAD se cierra, repite aparte las laminas que le quedaban,
  * dentro del mismo QCAD, antes de guardar, puede:
      - pasar el negro fijo a color 7 (se ve en fondo claro y oscuro),
      - quitar lineas y rellenos blancos (invisibles en papel),
      - enderezar las laminas giradas (/Rotate del PDF, que QCAD no aplica),
      - escalar el dibujo (1:N) y fijar las unidades,
  * en el modo "un solo DWG" pega todas las laminas lado a lado con su marco.

PyMuPDF solo se usa para contar las laminas, leer su tamano y giro, y la vista previa.
Requisitos: QCAD Professional (o su version de prueba) y pymupdf.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

try:  # PyMuPDF >= 1.24
    import pymupdf
except ImportError:  # versiones antiguas de PyMuPDF
    import fitz as pymupdf  # type: ignore

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
PT_A_MM = 25.4 / 72.0  # 1 punto PDF = 0.3528 mm (QCAD importa el PDF en mm de papel)

# unidades del dibujo: (nombre para QCAD, factor desde mm)
UNIDADES = {"mm": ("mm", 1.0), "cm": ("cm", 0.1), "m": ("m", 0.001)}

# version del DWG / DXF -> nombre de la version en QCAD (opcion -r de dwg2dwg)
VERSIONES_DWG = {
    "AutoCAD 2013": "R27",
    "AutoCAD 2018": "R32",
    "AutoCAD 2010": "R24",
    "AutoCAD 2007": "R21",
    "AutoCAD 2004": "R18",
    "AutoCAD 2000": "R15",
}
VERSION_DEFECTO = "R27"  # la que usa QCAD si no se indica
_ANIO_A_R = {"2018": "R32", "2013": "R27", "2010": "R24", "2007": "R21", "2004": "R18",
             "2000": "R15"}
_R_A_MAGIA = {"R32": "AC1032", "R27": "AC1027", "R24": "AC1024", "R21": "AC1021",
              "R18": "AC1018", "R15": "AC1015"}
_MAGIA_DWG = {"AC1015": "2000", "AC1018": "2004", "AC1021": "2007", "AC1024": "2010",
              "AC1027": "2013", "AC1032": "2018"}

QCAD_DIR_DEFECTO = r"C:\Program Files\QCAD" if os.name == "nt" else "/opt/qcad"

# limites de espera por proceso de QCAD (segundos sin ningun avance)
ESPERA_ARRANQUE = 240     # hasta que el script empieza (incluye la espera de la version de prueba)
ESPERA_LAMINA = 600       # por lamina
ESPERA_GUARDADO = 3600    # al guardar el archivo grande del modo "un solo DWG"
LAMINAS_POR_LOTE = 60     # laminas que convierte cada arranque de QCAD (como maximo)

# PyMuPDF no es seguro entre hilos: se usa con este bloqueo
BLOQUEO_PYMUPDF = threading.RLock()

# tamanos de papel conocidos (mm, lado corto x lado largo)
_PAPELES = [
    ("A0", 841, 1189), ("A1", 594, 841), ("A2", 420, 594), ("A3", 297, 420),
    ("A4", 210, 297), ("A5", 148, 210), ("Carta", 215.9, 279.4),
    ("Oficio", 215.9, 355.6), ("Tabloide", 279.4, 431.8),
    ("ARCH D", 609.6, 914.4), ("ARCH E", 914.4, 1219.2),
]


# ---------------------------------------------------------------------------
# Opciones y resultados
# ---------------------------------------------------------------------------
@dataclass
class Opciones:
    escala: float = 1.0             # 1:N del plano (1 = tamano real del papel)
    unidades: str = "mm"            # mm, cm o m
    negro_a_color7: bool = True     # negro fijo -> color 7 (blanco/negro segun el fondo)
    omitir_blancos: bool = True     # quita lineas y rellenos blancos (invisibles en papel)
    recortar: bool = True           # aplica las areas de recorte del PDF
    imagenes: bool = False          # importa imagenes (quedan en la carpeta pdf_images)
    marco_laminas: bool = True      # (modo unico) marco y numero de cada lamina
    laminas_por_fila: int = 0       # (modo unico) 0 = todas en una fila

    @property
    def factor(self) -> float:
        return float(self.escala) * UNIDADES[self.unidades][1]


@dataclass
class ResultadoLamina:
    pagina: int
    ok: bool = False
    archivo: str = ""
    entidades: int = 0
    segundos: float = 0.0
    error: str = ""
    avisos: list = field(default_factory=list)


class Cancelado(Exception):
    """La conversion fue cancelada por el usuario."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def nombre_archivo_valido(nombre: str) -> str:
    nombre = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nombre).strip(" .")
    return nombre or "plano"


def tamano_papel(ancho_mm: float, alto_mm: float) -> str:
    corto, largo = sorted((ancho_mm, alto_mm))
    orient = "horizontal" if ancho_mm > alto_mm else "vertical"
    for nombre, c, l in _PAPELES:
        if abs(corto - c) <= c * 0.02 and abs(largo - l) <= l * 0.02:
            return f"{nombre} {orient}"
    return f"{ancho_mm:.0f}x{alto_mm:.0f} mm"


def interpretar_rango(texto: str, total: int) -> list:
    """'1-3, 5, 8-' -> [0,1,2,4,7,...] (indices base 0). Vacio o 'todas' = todas."""
    texto = (texto or "").strip().lower()
    if texto in ("", "todas", "todo", "all", "*"):
        return list(range(total))
    texto = re.sub(r"\s*-\s*", "-", texto)  # "1 - 3" -> "1-3"
    paginas = []
    for parte in re.split(r"[,; ]+", texto):
        if not parte:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", parte)
        if m:
            ini = int(m.group(1)) if m.group(1) else 1
            fin = int(m.group(2)) if m.group(2) else total
        elif parte.isdigit():
            ini = fin = int(parte)
        else:
            raise ValueError(f"Rango de láminas no válido: '{parte}'")
        if ini > fin:
            ini, fin = fin, ini
        for p in range(max(ini, 1), min(fin, total) + 1):
            if p - 1 not in paginas:
                paginas.append(p - 1)
    if not paginas:
        raise ValueError("El rango no contiene láminas del PDF.")
    return paginas


def normalizar_version(version) -> str:
    """None, '2013', 'R27' o 'AutoCAD 2013' -> 'R27'."""
    if not version:
        return VERSION_DEFECTO
    v = str(version).strip()
    if v in VERSIONES_DWG:
        return VERSIONES_DWG[v]
    if v.upper() in _R_A_MAGIA:
        return v.upper()
    if v in _ANIO_A_R:
        return _ANIO_A_R[v]
    raise ValueError(f"Versión de DWG no válida: {version} (usa 2000, 2004, 2007, 2010, "
                     f"2013 o 2018)")


def info_pdf(ruta_o_bytes, password: str = "") -> dict:
    """Informacion rapida del PDF: numero de paginas y tamano de cada una."""
    with BLOQUEO_PYMUPDF:
        if isinstance(ruta_o_bytes, (bytes, bytearray)):
            doc = pymupdf.open(stream=bytes(ruta_o_bytes), filetype="pdf")
        else:
            doc = pymupdf.open(ruta_o_bytes)
        try:
            if doc.needs_pass and not doc.authenticate(password):
                return {"protegido": True, "paginas": 0, "detalle": []}
            detalle = []
            for i in range(doc.page_count):
                r = doc[i].rect
                w, h = r.width * PT_A_MM, r.height * PT_A_MM
                detalle.append({"pagina": i + 1, "ancho_mm": round(w, 1), "alto_mm": round(h, 1),
                                "papel": tamano_papel(w, h)})
            return {"protegido": False, "paginas": doc.page_count, "detalle": detalle,
                    "titulo": (doc.metadata or {}).get("title", "")}
        finally:
            doc.close()


def _geometria(page) -> dict:
    """Como enderezar la lamina que importa QCAD.

    QCAD usa las coordenadas del PDF tal cual (en mm) y no aplica el giro /Rotate de
    la pagina. Se devuelve el giro (grados, antihorario) y el desplazamiento que dejan
    la lamina como se ve en un visor, con su esquina inferior izquierda en (0, 0)."""
    mb = page.mediabox                       # coordenadas del PDF
    cb = page.cropbox                        # PyMuPDF la da con el eje Y invertido
    x0, x1 = cb.x0 * PT_A_MM, cb.x1 * PT_A_MM
    y0, y1 = (mb.y1 - cb.y1) * PT_A_MM, (mb.y1 - cb.y0) * PT_A_MM
    giro = (360 - int(page.rotation or 0)) % 360   # el visor gira en sentido horario
    a = math.radians(giro)
    c, s = round(math.cos(a)), round(math.sin(a))
    esquinas = [(x * c - y * s, x * s + y * c) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    xs, ys = [p[0] for p in esquinas], [p[1] for p in esquinas]
    return {"giro": giro, "dx": -min(xs), "dy": -min(ys),
            "ancho": max(xs) - min(xs), "alto": max(ys) - min(ys)}


def _paginas_info(ruta_pdf: str, password: str, indices: list) -> list:
    """Geometria y etiqueta de cada lamina pedida (en el orden de 'indices')."""
    with BLOQUEO_PYMUPDF:
        doc = pymupdf.open(ruta_pdf)
        try:
            if doc.needs_pass and not doc.authenticate(password):
                raise RuntimeError("El PDF tiene contraseña y no se pudo abrir.")
            res = []
            for i in indices:
                page = doc[i]
                g = _geometria(page)
                etiqueta = f"LÁMINA {i + 1}"
                try:
                    lbl = page.get_label()
                    if lbl and lbl != str(i + 1):
                        etiqueta += f" ({lbl})"
                except Exception:
                    pass
                g.update(n=i + 1, etiqueta=etiqueta)
                res.append(g)
            return res
        finally:
            doc.close()


def _pdf_para_qcad(ruta_pdf: str, password: str, carpeta: str) -> str:
    """PDF que QCAD puede abrir: el original, o una copia sin contraseña."""
    with BLOQUEO_PYMUPDF:
        doc = pymupdf.open(ruta_pdf)
        try:
            if not doc.needs_pass:
                return os.path.abspath(ruta_pdf)
            if not doc.authenticate(password):
                raise RuntimeError("El PDF tiene contraseña y no se pudo abrir.")
            copia = os.path.join(carpeta, "_sin_clave.pdf")
            doc.save(copia, encryption=pymupdf.PDF_ENCRYPT_NONE)
            return copia
        finally:
            doc.close()


def _enlazar(origen: str, destino: str) -> str:
    """Enlace duro (o copia) del PDF, para que QCAD deje las imagenes al lado."""
    try:
        os.link(origen, destino)
    except OSError:
        shutil.copyfile(origen, destino)
    return destino


def _disposicion(tamanos: list, por_fila: int):
    """Posicion (x, y inferior-izquierda) de cada lamina en el modo 'un solo DWG'.
    tamanos: lista de (ancho, alto) en unidades del dibujo. Filas de izquierda a
    derecha, de arriba hacia abajo."""
    n = len(tamanos)
    if n == 0:
        return []
    por_fila = n if por_fila <= 0 else por_fila
    sep = 0.1 * max(w for w, _ in tamanos)
    posiciones = []
    y_sup = 0.0
    for f in range(0, n, por_fila):
        fila = tamanos[f:f + por_fila]
        alto_fila = max(h for _, h in fila)
        x = 0.0
        for w, h in fila:
            posiciones.append((x, y_sup - h))
            x += w + sep
        y_sup -= alto_fila + sep * 1.5
    return posiciones


def _limpiar_ruta(ruta: str) -> str:
    return os.path.abspath((ruta or "").strip().strip('"').strip())


def version_dwg(ruta: str) -> str | None:
    """Version de un DWG segun su cabecera ('2013', '2018', ...)."""
    try:
        with open(ruta, "rb") as f:
            magia = f.read(6).decode("ascii", errors="replace")
    except OSError:
        return None
    return _MAGIA_DWG.get(magia, magia if magia.startswith("AC") else None)


def _cancelado(cancelar) -> bool:
    try:
        return bool(cancelar and cancelar())
    except Exception:
        return False


def _borrar(ruta: str):
    try:
        os.remove(ruta)
    except OSError:
        pass


def _mover(origen: str, destino: str, intentos: int = 20):
    """Mueve un archivo reemplazando el destino, tambien entre unidades distintas.
    Reintenta si otro programa (antivirus, OneDrive) lo bloquea un momento."""
    for n in range(intentos):
        try:
            try:
                os.replace(origen, destino)
            except PermissionError:
                raise
            except OSError:
                # otra unidad: se copia con un nombre temporal y luego se renombra
                tmp = f"{destino}.{uuid.uuid4().hex[:6]}.tmp"
                try:
                    shutil.copyfile(origen, tmp)
                    os.replace(tmp, destino)
                finally:
                    _borrar(tmp)
                _borrar(origen)
            return
        except PermissionError:
            if n == intentos - 1:
                raise
            time.sleep(0.5)


def _juntar_imagenes(origen_dir: str, destino_dir: str):
    """Pasa las imagenes que QCAD dejo en origen_dir/pdf_images a destino_dir/pdf_images.
    Los DWG las enlazan con la ruta relativa pdf_images\\<archivo>."""
    src = os.path.join(origen_dir, "pdf_images")
    if not os.path.isdir(src):
        return
    dst = os.path.join(destino_dir, "pdf_images")
    os.makedirs(dst, exist_ok=True)
    for nombre in os.listdir(src):
        a, b = os.path.join(src, nombre), os.path.join(dst, nombre)
        if os.path.isfile(a):
            if os.path.exists(b):      # misma imagen (el nombre depende del contenido)
                _borrar(a)
            else:
                try:
                    _mover(a, b)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# QCAD
# ---------------------------------------------------------------------------
def diagnostico_qcad(qcad_dir: str) -> tuple:
    """(True, mensaje) si en la carpeta hay un QCAD capaz de importar PDF."""
    qcad_dir = _limpiar_ruta(qcad_dir)
    if not os.path.isdir(qcad_dir):
        return False, f"No existe la carpeta {qcad_dir}."
    exe = os.path.join(qcad_dir, "qcadcmd.com" if os.name == "nt" else "qcad")
    if not os.path.isfile(exe):
        return False, f"No se encontró {os.path.basename(exe)} de QCAD en {qcad_dir}."
    plugins = os.path.join(qcad_dir, "plugins")
    try:
        nombres = [n.lower() for n in os.listdir(plugins)]
    except OSError:
        nombres = []
    if not any("qcadpdf" in n for n in nombres):
        return False, ("Esta instalación de QCAD no trae el importador de PDF: hace falta "
                       "QCAD Professional (o su versión de prueba).")
    return True, "QCAD encontrado."


def qcad_disponible(qcad_dir: str) -> bool:
    return diagnostico_qcad(qcad_dir)[0]


def _ruta_qcad(ruta: str) -> str:
    """Ruta tal como la debe ver QCAD (con '/'). Con QCAD_RUTAS_WINE=1 (solo para
    pruebas con QCAD bajo Wine) las rutas se pasan por la unidad Z:."""
    ruta = os.path.abspath(ruta)
    if os.environ.get("QCAD_RUTAS_WINE") == "1" and ruta.startswith("/"):
        return "Z:" + ruta
    return ruta.replace("\\", "/")


def _comando_qcad(qcad_dir: str, script: str) -> list:
    exe = os.path.join(qcad_dir, "qcadcmd.com" if os.name == "nt" else "qcad")
    # mismas opciones que usan pdf2dwg.bat / dwg2dwg.bat
    return [exe, "-no-gui", "-allow-multiple-instances", "-autostart", _ruta_qcad(script)]


def _terminar_arbol(p):
    """Termina un proceso de QCAD y sus hijos."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.killpg(p.pid, 9)  # grupo propio (start_new_session)
            except Exception:
                p.kill()
    except Exception:
        pass
    try:
        p.wait(timeout=30)
    except Exception:
        pass


# Script que corre dentro de QCAD. CFG trae el PDF, las laminas y las opciones.
# Avisa su avance agregando lineas a un archivo (QCAD no vacia su salida estandar).
_PLANTILLA_JS = r"""
include("scripts/library.js");

var CFG = __CFG__;

function registrar(texto) {
    for (var intento = 0; intento < 50; intento++) {
        var f = new QFile(CFG.progreso);
        if (f.open(makeQIODeviceOpenMode(QIODevice.WriteOnly, QIODevice.Append, QIODevice.Text))) {
            var ts = new QTextStream(f);
            setUtf8Codec(ts);
            ts.writeString(texto + "\n");
            f.close();
            return;
        }
        var t = Date.now();
        while (Date.now() - t < 20) { }   // el archivo puede estar ocupado un instante
    }
}

function limpio(texto) {
    return String(texto).replace(/[\r\n|]+/g, " ");
}

function cancelado() {
    return CFG.bandera !== "" && new QFileInfo(CFG.bandera).exists();
}

function unidad() {
    if (CFG.unidad === "cm") { return RS.Centimeter; }
    if (CFG.unidad === "m") { return RS.Meter; }
    return RS.Millimeter;
}

function importar(numero) {
    var doc = new RDocument(new RMemoryStorage(), createSpatialIndex());
    var di = new RDocumentInterface(doc);
    var p = {};
    p["NoGui"] = true;
    p["PdfPage"] = numero;
    p["PdfImages"] = CFG.imagenes;
    p["PdfRelPath"] = CFG.imagenes;
    p["PdfClipping"] = CFG.recortar;
    if (di.importFile(CFG.pdf, "", false, p) !== RDocumentInterface.IoErrorNoError) {
        destr(di);
        return null;
    }
    return di;
}

function esTexto(e) {
    if (typeof isTextBasedEntity === "function") { return isTextBasedEntity(e); }
    return e.getType() === RS.EntityText;
}

// colores, blancos, giro, posicion y escala; devuelve cuantas entidades quedan
function ajustar(di, pag) {
    var doc = di.getDocument();
    var ids = doc.queryAllEntities(false, false);
    var borrar = new RDeleteObjectsOperation();
    var cambiar = new RAddObjectsOperation();
    var nBorrar = 0, nCambiar = 0;
    var ang = pag.giro * Math.PI / 180.0;
    var mover = (Math.abs(pag.dx) > 1e-9 || Math.abs(pag.dy) > 1e-9);
    var origen = new RVector(0, 0);
    for (var k = 0; k < ids.length; k++) {
        var e = doc.queryEntity(ids[k]);
        if (isNull(e)) { continue; }
        var c = e.getColor();
        var fijo = !c.isByLayer() && !c.isByBlock();
        if (CFG.omitirBlancos && fijo && c.red() === 255 && c.green() === 255 && c.blue() === 255
                && !esTexto(e)) {
            borrar.deleteObject(e);
            nBorrar++;
            continue;
        }
        var cambio = false;
        if (CFG.negroA7 && fijo && c.red() === 0 && c.green() === 0 && c.blue() === 0) {
            e.setColor(new RColor("white"));   // "white" de QCAD = color 7 en DWG
            cambio = true;
        }
        if (ang !== 0) { e.rotate(ang, origen); cambio = true; }
        if (mover) { e.move(new RVector(pag.dx, pag.dy)); cambio = true; }
        if (CFG.factor !== 1) { e.scale(new RVector(CFG.factor, CFG.factor), origen); cambio = true; }
        if (cambio) { cambiar.addObject(e, false); nCambiar++; }
    }
    if (nBorrar > 0) { di.applyOperation(borrar); }
    if (nCambiar > 0) { di.applyOperation(cambiar); }
    return ids.length - nBorrar;
}

function porLamina() {
    for (var i = 0; i < CFG.paginas.length; i++) {
        if (cancelado()) { registrar("CANCELADO"); return; }
        var pag = CFG.paginas[i];
        registrar("INICIO|" + pag.n);
        var t0 = Date.now();
        try {
            var di = importar(pag.n);
            if (di === null) {
                registrar("MAL|" + pag.n + "|QCAD no pudo leer la lámina");
                continue;
            }
            var n = ajustar(di, pag);
            di.getDocument().setUnit(unidad());
            var ok = di.exportFile(pag.salida, CFG.version);
            destr(di);
            if (ok) {
                registrar("OK|" + pag.n + "|" + n + "|" + ((Date.now() - t0) / 1000).toFixed(2));
            } else {
                registrar("MAL|" + pag.n + "|QCAD no pudo guardar el archivo");
            }
        } catch (ex) {
            registrar("MAL|" + pag.n + "|Error en QCAD: " + limpio(ex));
        }
    }
}

function marco(docM, diM, capaId, pag) {
    var pl = new RPolyline();
    pl.appendVertex(new RVector(pag.ox, pag.oy));
    pl.appendVertex(new RVector(pag.ox + pag.ancho, pag.oy));
    pl.appendVertex(new RVector(pag.ox + pag.ancho, pag.oy + pag.alto));
    pl.appendVertex(new RVector(pag.ox, pag.oy + pag.alto));
    pl.setClosed(true);
    var ent = new RPolylineEntity(docM, new RPolylineData(pl));
    ent.setLayerId(capaId);
    var alto = Math.max(Math.min(pag.ancho, pag.alto) * 0.02, 1e-6);
    var pos = new RVector(pag.ox, pag.oy + pag.alto + alto * 0.6);
    var td = new RTextData(pos, pos, alto, 0, RS.VAlignBottom, RS.HAlignLeft, RS.LeftToRight,
                           RS.Exact, 1.0, pag.etiqueta, "Arial", false, false, 0, false);
    var tx = new RTextEntity(docM, td);
    tx.setLayerId(capaId);
    var op = new RAddObjectsOperation();
    op.addObject(ent, false);
    op.addObject(tx, false);
    diM.applyOperation(op);
}

function unico() {
    var docM = new RDocument(new RMemoryStorage(), createSpatialIndex());
    var diM = new RDocumentInterface(docM);
    var capaId = -1;
    if (CFG.marco) {
        var capa = new RLayer(docM, "LAMINAS", false, false, new RColor("gray"),
                              docM.getLinetypeId("CONTINUOUS"), RLineweight.Weight025, false);
        var opc = new RAddObjectsOperation();
        opc.addObject(capa);
        diM.applyOperation(opc);
        capaId = docM.getLayerId("LAMINAS");
    }
    var total = 0, pegadas = 0;
    for (var i = 0; i < CFG.paginas.length; i++) {
        if (cancelado()) { registrar("CANCELADO"); destr(diM); return; }
        var pag = CFG.paginas[i];
        registrar("INICIO|" + pag.n);
        var t0 = Date.now();
        try {
            var di = importar(pag.n);
            if (di === null) {
                registrar("MAL|" + pag.n + "|QCAD no pudo leer la lámina");
                continue;
            }
            var n = ajustar(di, pag);
            var op = new RPasteOperation(di.getDocument());
            op.setOffset(new RVector(pag.ox, pag.oy));
            op.setScale(1.0);
            op.setRotation(0);
            op.setFlipHorizontal(false);
            op.setFlipVertical(false);
            op.setToCurrentLayer(false);
            op.setOverwriteLayers(false);
            op.setOverwriteBlocks(false);
            diM.applyOperation(op);
            destr(di);
            if (CFG.marco) { marco(docM, diM, capaId, pag); }
            total += n;
            pegadas++;
            registrar("OK|" + pag.n + "|" + n + "|" + ((Date.now() - t0) / 1000).toFixed(2));
        } catch (ex) {
            registrar("MAL|" + pag.n + "|Error en QCAD: " + limpio(ex));
        }
    }
    if (pegadas === 0) {
        registrar("UNICO|MAL|No se pudo convertir ninguna lámina");
        destr(diM);
        return;
    }
    docM.setUnit(unidad());
    registrar("EXPORTANDO");
    var ok = diM.exportFile(CFG.salidaUnico, CFG.version);
    destr(diM);
    registrar(ok ? ("UNICO|OK|" + total) : "UNICO|MAL|QCAD no pudo guardar el archivo");
}

registrar("SCRIPT|" + RSettings.getVersionString());
if (CFG.modo === "unico") { unico(); } else { porLamina(); }
registrar("FIN");
"""


def _cfg_base(op: Opciones, version: str, modo: str) -> dict:
    return {"version": version, "imagenes": bool(op.imagenes), "recortar": bool(op.recortar),
            "negroA7": bool(op.negro_a_color7), "omitirBlancos": bool(op.omitir_blancos),
            "factor": float(op.factor), "unidad": UNIDADES[op.unidades][0], "modo": modo,
            "marco": bool(op.marco_laminas)}


_JOB = None
_JOB_BLOQUEO = threading.Lock()


def _job_windows():
    """Objeto de trabajo de Windows que cierra los QCAD si este programa termina de
    golpe (p. ej. al cerrar la ventana): QCAD corre sin ventana y no se enteraria."""
    global _JOB
    if os.name != "nt":
        return None
    with _JOB_BLOQUEO:
        if _JOB is None:
            try:
                import ctypes
                from ctypes import wintypes

                class _IoCounters(ctypes.Structure):
                    _fields_ = [(n, ctypes.c_ulonglong) for n in (
                        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

                class _LimiteBasico(ctypes.Structure):
                    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                                ("PerJobUserTimeLimit", ctypes.c_int64),
                                ("LimitFlags", wintypes.DWORD),
                                ("MinimumWorkingSetSize", ctypes.c_size_t),
                                ("MaximumWorkingSetSize", ctypes.c_size_t),
                                ("ActiveProcessLimit", wintypes.DWORD),
                                ("Affinity", ctypes.c_size_t),
                                ("PriorityClass", wintypes.DWORD),
                                ("SchedulingClass", wintypes.DWORD)]

                class _LimiteExtendido(ctypes.Structure):
                    _fields_ = [("BasicLimitInformation", _LimiteBasico),
                                ("IoInfo", _IoCounters),
                                ("ProcessMemoryLimit", ctypes.c_size_t),
                                ("JobMemoryLimit", ctypes.c_size_t),
                                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                                ("PeakJobMemoryUsed", ctypes.c_size_t)]

                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateJobObjectW.restype = wintypes.HANDLE
                k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
                k32.SetInformationJobObject.restype = wintypes.BOOL
                k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                        ctypes.c_void_p, wintypes.DWORD]
                k32.AssignProcessToJobObject.restype = wintypes.BOOL
                k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
                job = k32.CreateJobObjectW(None, None)
                if not job:
                    raise OSError("CreateJobObject")
                info = _LimiteExtendido()
                info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
                if not k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                                   ctypes.sizeof(info)):
                    raise OSError("SetInformationJobObject")
                _JOB = (k32, job)
            except Exception:
                _JOB = False
        return _JOB or None


def _asociar_job(p):
    j = _job_windows()
    if j:
        try:
            j[0].AssignProcessToJobObject(j[1], int(p._handle))
        except Exception:
            pass


class _Procesos:
    """Procesos de QCAD en marcha (para poder cerrarlos todos si se interrumpe)."""

    def __init__(self):
        self.bloqueo = threading.Lock()
        self.activos = set()
        self.cerrado = False

    def agregar(self, p) -> bool:
        with self.bloqueo:
            if not self.cerrado:
                self.activos.add(p)
                return True
        _terminar_arbol(p)  # ya se pidio detener todo
        return False

    def quitar(self, p):
        with self.bloqueo:
            self.activos.discard(p)

    def cerrar(self):
        """Cierra los QCAD en marcha y no deja lanzar otros."""
        with self.bloqueo:
            self.cerrado = True
            procesos = list(self.activos)
        for p in procesos:
            _terminar_arbol(p)


def _cola_log(ruta: str, n: int = 800) -> tuple:
    """(ultimas lineas utiles de la salida de QCAD, True si es la version de prueba)."""
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
    except OSError:
        return "", False
    texto = datos.decode("utf-8", errors="replace")
    prueba = "trial version" in texto
    utiles = [l for l in texto.splitlines()
              if l.strip() and "Debug:" not in l and "Warning:" not in l]
    return "\n".join(utiles)[-n:].strip(), prueba


def _evento_qcad(campos: list, res: dict, al_evento):
    """Interpreta una linea de avance del script de QCAD."""
    clave = campos[0]
    if clave == "SCRIPT":
        res["arranco"] = True
    elif clave == "INICIO":
        res["en_curso"] = int(campos[1])
    elif clave == "OK":
        n, entidades, segundos = int(campos[1]), int(campos[2]), float(campos[3])
        res["hechas"].add(n)
        res["en_curso"] = None
        al_evento(("ok", n, entidades, segundos))
    elif clave == "MAL":
        n = int(campos[1])
        res["hechas"].add(n)
        res["en_curso"] = None
        al_evento(("mal", n, "|".join(campos[2:]) or "Error en QCAD"))
    elif clave == "EXPORTANDO":
        al_evento(("exportando",))
    elif clave == "UNICO":
        res["unico"] = (campos[1], "|".join(campos[2:]))
    elif clave == "CANCELADO":
        res["cancelado"] = True
    elif clave == "FIN":
        res["fin"] = True


def _correr_qcad(qcad_dir: str, cfg: dict, carpeta: str, al_evento, cancelar, bandera: str,
                 procesos: _Procesos | None = None) -> dict:
    """Ejecuta un QCAD con el script de 'cfg' y avisa cada lamina terminada con
    al_evento(evento). Devuelve un resumen de como termino."""
    uid = uuid.uuid4().hex[:8]
    avance = os.path.join(carpeta, f"_avance_{uid}.txt")
    script = os.path.join(carpeta, f"_lote_{uid}.js")
    log = os.path.join(carpeta, f"_qcad_{uid}.log")
    cfg = dict(cfg, progreso=_ruta_qcad(avance), bandera=_ruta_qcad(bandera) if bandera else "")
    texto = _PLANTILLA_JS.replace("__CFG__", json.dumps(cfg, ensure_ascii=True))
    with open(script, "w", encoding="ascii") as f:   # acentos como \xNN: no depende de la codificacion
        f.write(texto.encode("ascii", "backslashreplace").decode("ascii"))
    open(avance, "wb").close()
    res = {"arranco": False, "fin": False, "en_curso": None, "hechas": set(), "colgado": False,
           "cancelado": False, "unico": None, "codigo": None, "salida": "", "prueba": False}
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    with open(log, "wb") as flog:
        try:
            p = subprocess.Popen(_comando_qcad(qcad_dir, script), cwd=qcad_dir,
                                 stdin=subprocess.DEVNULL, stdout=flog, stderr=subprocess.STDOUT,
                                 **kw)
        except OSError as ex:
            res["salida"] = f"No se pudo ejecutar QCAD: {ex}"
            return res
        _asociar_job(p)
        if procesos:
            procesos.agregar(p)
        try:
            leido, resto = 0, b""
            ultimo = time.time()
            exportando = False
            pedido_cancelar = None
            while True:
                vivo = p.poll() is None
                try:
                    with open(avance, "rb") as f:
                        f.seek(leido)
                        datos = f.read()
                except OSError:
                    datos = b""
                if datos:
                    leido += len(datos)
                    partes = (resto + datos).split(b"\n")
                    resto = partes.pop()
                    for linea in partes:
                        campos = linea.decode("utf-8", errors="replace").strip("\r").split("|")
                        ultimo = time.time()
                        if campos[0] == "EXPORTANDO":
                            exportando = True
                        try:
                            _evento_qcad(campos, res, al_evento)
                        except (ValueError, IndexError):
                            pass  # linea incompleta o dañada: se ignora
                if not vivo:
                    break
                ahora = time.time()
                if _cancelado(cancelar):
                    res["cancelado"] = True
                    if pedido_cancelar is None:
                        pedido_cancelar = ahora
                        try:  # QCAD lo revisa entre lamina y lamina
                            open(bandera, "w").close()
                        except OSError:
                            pass
                    elif ahora - pedido_cancelar > 15:
                        _terminar_arbol(p)
                        continue
                limite = ESPERA_ARRANQUE if not res["arranco"] else \
                    (ESPERA_GUARDADO if exportando else ESPERA_LAMINA)
                if ahora - ultimo > limite:
                    res["colgado"] = True
                    _terminar_arbol(p)
                    continue
                time.sleep(0.3)
        finally:
            if p.poll() is None:
                _terminar_arbol(p)
            if procesos:
                procesos.quitar(p)
        res["codigo"] = p.returncode
    res["salida"], res["prueba"] = _cola_log(log)
    return res


def _revisar_version(archivo: str, version: str, avisos: list):
    """Aviso si QCAD guardo el DWG en otra version que la pedida."""
    if not archivo.lower().endswith(".dwg"):
        return
    esperada = _MAGIA_DWG.get(_R_A_MAGIA.get(version, ""))
    real = version_dwg(archivo)
    if esperada and real and real != esperada:
        avisos.append(f"QCAD guardó los DWG en formato AutoCAD {real} (se pidió {esperada}).")


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def convertir_por_lamina(ruta_pdf: str, indices: list, carpeta: str, nombre_base: str,
                         op: Opciones, formato: str = "dwg", qcad_dir: str = QCAD_DIR_DEFECTO,
                         version: str | None = None, procesos: int = 0, password: str = "",
                         progreso=None, cancelar=None, digitos: int | None = None,
                         avisos: list | None = None) -> list:
    """Convierte cada lamina en su propio archivo con QCAD, en la carpeta indicada.
    Devuelve una lista de ResultadoLamina (sin las laminas no procesadas si se cancela).
    progreso(hechas, total, resultado) se llama al terminar cada lamina."""
    avisos = avisos if avisos is not None else []
    qcad_dir = _limpiar_ruta(qcad_dir)
    version = normalizar_version(version)
    if not indices:
        return []
    ok_qcad, msg = diagnostico_qcad(qcad_dir)
    if not ok_qcad:
        raise RuntimeError(msg)
    if not digitos:
        digitos = max(2, len(str(max(indices) + 1)))
    ext = ".dwg" if formato == "dwg" else ".dxf"
    os.makedirs(carpeta, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="_qcad_", dir=carpeta)
    bloqueo = threading.Lock()
    resultados: dict = {}
    hechas = [0]
    total = len(indices)
    estado = {"abortar": None, "prueba": False, "version_revisada": False,
              "reintentadas": set()}
    vivos = _Procesos()
    detener = threading.Event()

    def parar() -> bool:
        return detener.is_set() or _cancelado(cancelar)

    try:
        geo = _paginas_info(ruta_pdf, password, indices)
        pdf = _pdf_para_qcad(ruta_pdf, password, tmp)
        if procesos <= 0:
            procesos = min(4, os.cpu_count() or 1)
        tam = max(5, min(LAMINAS_POR_LOTE, math.ceil(total / max(1, procesos))))
        cola = deque(geo[k:k + tam] for k in range(0, total, tam))
        procesos = max(1, min(procesos, len(cola)))
        bandera = os.path.join(tmp, "_cancelar")
        cfg_base = _cfg_base(op, version, "lamina")

        def terminar(n, ok, archivo="", entidades=0, segundos=0.0, error=""):
            with bloqueo:
                if n in resultados:
                    return
                r = ResultadoLamina(pagina=n, ok=ok, archivo=archivo, entidades=entidades,
                                    segundos=round(segundos, 2), error=error)
                resultados[n] = r
                hechas[0] += 1
                if ok and not estado["version_revisada"]:
                    estado["version_revisada"] = True
                    _revisar_version(archivo, version, avisos)
                if progreso:
                    try:
                        progreso(hechas[0], total, r)
                    except Exception:
                        pass

        def trabajador(w):
            choques = 0
            while True:
                with bloqueo:
                    if estado["abortar"] or parar() or not cola:
                        return
                    lote = cola.popleft()
                try:
                    choques = convertir_lote(w, lote, choques)
                except Exception as ex:  # error inesperado: se informa en cada lamina del lote
                    for g in lote:
                        terminar(g["n"], False, error=f"Error al ejecutar QCAD: {ex}")
                if choques is None:
                    return

        def convertir_lote(w, lote, choques):
            """Convierte un lote con un QCAD. Devuelve el contador de cierres seguidos
            de QCAD, o None si hay que detener todo."""
            dir_lote = os.path.join(tmp, f"lote{w}_{uuid.uuid4().hex[:6]}")
            os.makedirs(dir_lote)
            pdf_lote = _enlazar(pdf, os.path.join(dir_lote, nombre_base + ".pdf")) \
                if op.imagenes else pdf
            nombres = {g["n"]: f"{nombre_base}_L{g['n']:0{digitos}d}{ext}" for g in lote}
            paginas = [dict(g, salida=_ruta_qcad(os.path.join(dir_lote, nombres[g["n"]])))
                       for g in lote]

            def al_evento(ev):
                if ev[0] == "ok":
                    n = ev[1]
                    origen = os.path.join(dir_lote, nombres[n])
                    if not (os.path.isfile(origen) and os.path.getsize(origen) > 0):
                        terminar(n, False, error="QCAD no generó el archivo.")
                        return
                    destino = os.path.join(carpeta, nombres[n])
                    try:
                        _mover(origen, destino)
                    except OSError as ex:
                        terminar(n, False, error=f"No se pudo guardar {destino}: {ex}")
                        return
                    terminar(n, True, destino, ev[2], ev[3])
                elif ev[0] == "mal":
                    terminar(ev[1], False, error=ev[2])

            res = _correr_qcad(qcad_dir, dict(cfg_base, pdf=_ruta_qcad(pdf_lote), paginas=paginas),
                               dir_lote, al_evento, parar, bandera, vivos)
            if res["prueba"]:
                estado["prueba"] = True
            if op.imagenes:   # al final del lote: QCAD ya no las usa
                with bloqueo:
                    _juntar_imagenes(dir_lote, carpeta)
            pendientes = [g for g in lote if g["n"] not in res["hechas"]]
            if parar():
                return choques
            if not pendientes:
                shutil.rmtree(dir_lote, ignore_errors=True)
                return 0
            if not res["arranco"]:
                # QCAD ni siquiera empezo: el problema es general, se detiene todo
                motivo = "QCAD no arrancó"
                if res["colgado"]:
                    motivo += f" (sin respuesta en {ESPERA_ARRANQUE} s)"
                if res["salida"]:
                    motivo += f": {res['salida'][-400:]}"
                with bloqueo:
                    estado["abortar"] = motivo
                for g in pendientes:
                    terminar(g["n"], False, error=motivo)
                return None
            culpable = res["en_curso"]
            reintento = None
            if culpable is not None:
                if res["colgado"]:
                    terminar(culpable, False, error="QCAD dejó de responder con esta lámina.")
                elif culpable not in estado["reintentadas"] and len(lote) > 1:
                    # QCAD se cerro: la lamina se repite una vez, sola, al final
                    estado["reintentadas"].add(culpable)
                    reintento = [g for g in lote if g["n"] == culpable]
                else:
                    terminar(culpable, False, error="QCAD se cerró al convertir esta lámina.")
            resto = [g for g in pendientes if g["n"] != culpable]
            if reintento:
                with bloqueo:
                    cola.append(reintento)
            # un cierre durante una lamina saca esa lamina de la cola (siempre avanza);
            # si QCAD se cierra sin estar en ninguna, podria repetirse sin fin
            choques = 0 if culpable is not None else choques + 1
            if choques >= 3:
                motivo = "QCAD se cierra una y otra vez; se detuvo la conversión."
                if res["salida"]:
                    motivo += " " + res["salida"][-300:]
                with bloqueo:
                    estado["abortar"] = motivo
                for g in resto:
                    terminar(g["n"], False, error=motivo)
                return None
            if resto:
                with bloqueo:
                    cola.appendleft(resto)  # se repiten en un QCAD nuevo
            return choques

        hilos = [threading.Thread(target=trabajador, args=(w,), daemon=True)
                 for w in range(procesos)]
        for h in hilos:
            h.start()
        try:
            for h in hilos:
                while h.is_alive():
                    h.join(0.5)
        except BaseException:
            # interrupcion (Ctrl+C): no se lanzan mas QCAD y se cierran los que corren
            detener.set()
            try:
                open(bandera, "w").close()
            except OSError:
                pass
            vivos.cerrar()
            for h in hilos:
                h.join(30)
            raise
        if estado["abortar"]:
            for lote in cola:
                for g in lote:
                    terminar(g["n"], False, error=estado["abortar"])
        if estado["prueba"]:
            avisos.append("QCAD está en versión de prueba: espera 15 s cada vez que se abre "
                          "(la conversión lo abre pocas veces, por lotes).")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return [resultados[i + 1] for i in indices if i + 1 in resultados]


def convertir_unico(ruta_pdf: str, indices: list, ruta_salida: str, op: Opciones,
                    formato: str = "dwg", qcad_dir: str = QCAD_DIR_DEFECTO,
                    version: str | None = None, procesos: int = 0, password: str = "",
                    progreso=None, cancelar=None, etapa=None):
    """Todas las laminas en un solo DWG/DXF, lado a lado (un solo QCAD las importa y
    las pega). Devuelve (lista de ResultadoLamina, avisos)."""
    avisos: list = []
    qcad_dir = _limpiar_ruta(qcad_dir)
    version = normalizar_version(version)
    ok_qcad, msg = diagnostico_qcad(qcad_dir)
    if not ok_qcad:
        raise RuntimeError(msg)
    ruta_salida = os.path.abspath(ruta_salida)
    carpeta = os.path.dirname(ruta_salida)
    os.makedirs(carpeta, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="_qcad_", dir=carpeta)
    vivos = _Procesos()
    try:
        geo = _paginas_info(ruta_pdf, password, indices)
        f = op.factor
        tamanos = [(g["ancho"] * f, g["alto"] * f) for g in geo]
        paginas = []
        for g, (x, y), (w, h) in zip(geo, _disposicion(tamanos, op.laminas_por_fila), tamanos):
            paginas.append(dict(g, ox=x, oy=y, ancho=w, alto=h))
        pdf = _pdf_para_qcad(ruta_pdf, password, tmp)
        if op.imagenes:
            base = os.path.splitext(os.path.basename(ruta_salida))[0]
            pdf = _enlazar(pdf, os.path.join(tmp, base + ".pdf"))
        salida_tmp = os.path.join(tmp, os.path.basename(ruta_salida))
        bandera = os.path.join(tmp, "_cancelar")
        total = len(paginas)
        estado: dict = {}          # pagina -> ResultadoLamina
        reportadas: set = set()    # laminas ya avisadas a 'progreso'
        excluidas: list = []
        max_excluidas = max(3, math.ceil(0.1 * total))

        def al_evento(ev):
            if ev[0] in ("ok", "mal"):
                n = ev[1]
                if ev[0] == "ok":
                    r = ResultadoLamina(pagina=n, ok=True, entidades=ev[2], segundos=ev[3])
                else:
                    r = ResultadoLamina(pagina=n, ok=False, error=ev[2])
                estado[n] = r
                if progreso and n not in reportadas:   # en un reintento no se cuenta de nuevo
                    reportadas.add(n)
                    try:
                        progreso(len(reportadas), total, r)
                    except Exception:
                        pass
            elif ev[0] == "exportando" and etapa:
                etapa("Guardando el archivo con QCAD…")

        listo = False
        while True:
            pags = [p for p in paginas if p["n"] not in excluidas]
            if not pags:
                break
            cfg = dict(_cfg_base(op, version, "unico"), pdf=_ruta_qcad(pdf), paginas=pags,
                       salidaUnico=_ruta_qcad(salida_tmp))
            try:
                res = _correr_qcad(qcad_dir, cfg, tmp, al_evento, cancelar, bandera, vivos)
            except BaseException:
                vivos.cerrar()
                raise
            aviso_prueba = "QCAD está en versión de prueba: espera 15 s cada vez que se abre."
            if res["prueba"] and aviso_prueba not in avisos:
                avisos.append(aviso_prueba)
            if _cancelado(cancelar) or res["cancelado"]:
                raise Cancelado("Conversión cancelada.")
            if res["unico"] and res["unico"][0] == "OK" and os.path.isfile(salida_tmp):
                listo = True
                break
            if not res["arranco"]:
                raise RuntimeError("QCAD no arrancó: " + (res["salida"][-400:] or "sin respuesta"))
            if res["unico"] and res["unico"][0] == "MAL":
                raise RuntimeError(res["unico"][1])
            culpable = res["en_curso"]
            if culpable is None:
                raise RuntimeError("QCAD se cerró al guardar el archivo. " + res["salida"][-300:])
            excluidas.append(culpable)
            estado[culpable] = ResultadoLamina(
                pagina=culpable, ok=False,
                error="QCAD dejó de responder con esta lámina." if res["colgado"]
                else "QCAD se cerró al convertir esta lámina.")
            if len(excluidas) > max_excluidas:
                raise RuntimeError("QCAD falló con demasiadas láminas ("
                                   + ", ".join(map(str, excluidas)) + "); conviértelas aparte "
                                   "o usa el modo de un DWG por lámina.")
            avisos.append(f"QCAD falló con la lámina {culpable}; se repitió sin ella.")
        if not listo:
            raise RuntimeError("No se pudo crear el archivo con QCAD"
                               + (f" (falló con las láminas {', '.join(map(str, excluidas))})."
                                  if excluidas else "."))
        _mover(salida_tmp, ruta_salida)
        if op.imagenes:
            _juntar_imagenes(tmp, carpeta)
        _revisar_version(ruta_salida, version, avisos)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    resultados = []
    for i in indices:
        r = estado.get(i + 1)
        if r is None:
            continue
        if r.ok:
            r.archivo = ruta_salida
        resultados.append(r)
    return resultados, avisos


_CACHE_PRUEBA_QCAD: dict = {}


def probar_qcad(qcad_dir: str, version: str | None = None, cancelar=None):
    """Convierte un PDF minimo para comprobar que QCAD funciona. Devuelve
    (version, avisos). Lanza RuntimeError si QCAD no funciona."""
    qcad_dir = _limpiar_ruta(qcad_dir)
    version = normalizar_version(version)
    clave = (qcad_dir, version)
    if clave in _CACHE_PRUEBA_QCAD:
        return _CACHE_PRUEBA_QCAD[clave]
    ok_qcad, msg = diagnostico_qcad(qcad_dir)
    if not ok_qcad:
        raise RuntimeError(msg)
    carpeta = tempfile.mkdtemp(prefix="prueba_qcad_")
    try:
        pdf = os.path.join(carpeta, "prueba.pdf")
        with BLOQUEO_PYMUPDF:
            d = pymupdf.open()
            pg = d.new_page(width=200, height=100)
            pg.draw_line((20, 60), (180, 60))
            pg.insert_text((20, 45), "QCAD", fontsize=14)
            d.save(pdf)
            d.close()
        avisos: list = []
        res = convertir_por_lamina(pdf, [0], os.path.join(carpeta, "salida"), "prueba",
                                   Opciones(), "dwg", qcad_dir, version, 1, cancelar=cancelar,
                                   avisos=avisos)
        if _cancelado(cancelar):
            raise Cancelado("Conversión cancelada.")
        if not res or not res[0].ok:
            raise RuntimeError(res[0].error if res else "QCAD no convirtió el dibujo de prueba.")
        if version_dwg(res[0].archivo) is None:
            raise RuntimeError("QCAD generó un archivo que no es DWG.")
        resultado = (version, avisos)
        _CACHE_PRUEBA_QCAD[clave] = resultado
        return resultado
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


# ---------------------------------------------------------------------------
# Uso por linea de comandos: conversion por lotes sin abrir la app
#   python conversor.py planos --formato dwg -o planos\DWG_QCAD --saltar-existentes
# ---------------------------------------------------------------------------
def _clave_natural(texto: str):
    """Orden natural: 'L2' antes que 'L10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", texto)]


def buscar_pdfs(entradas) -> tuple:
    """Expande las carpetas a los PDF que contienen (sin subcarpetas), en orden natural.
    Devuelve (lista de rutas, True si alguna entrada era una carpeta)."""
    pdfs, hubo_carpeta, vistos = [], False, set()
    for e in entradas:
        ruta = _limpiar_ruta(e)
        if os.path.isdir(ruta):
            hubo_carpeta = True
            nombres = sorted((n for n in os.listdir(ruta) if n.lower().endswith(".pdf")),
                             key=_clave_natural)
            candidatos = [os.path.join(ruta, n) for n in nombres
                          if os.path.isfile(os.path.join(ruta, n))]
        else:
            candidatos = [ruta]
        for c in candidatos:
            if os.path.normcase(c) not in vistos:
                vistos.add(os.path.normcase(c))
                pdfs.append(c)
    return pdfs, hubo_carpeta


def archivo_terminado(ruta: str) -> bool:
    """True si 'ruta' es un DWG/DXF completo (para continuar una conversion interrumpida)."""
    try:
        if os.path.getsize(ruta) < 64:
            return False
        if ruta.lower().endswith(".dwg"):
            return version_dwg(ruta) is not None
        with open(ruta, "rb") as f:
            f.seek(-64, os.SEEK_END)
            return b"EOF" in f.read()
    except OSError:
        return False


def _duracion(segundos: float) -> str:
    s = int(round(max(segundos, 0)))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


class _Registro:
    """Muestra los mensajes en pantalla y los guarda en un archivo de registro."""

    def __init__(self, ruta: str | None = None):
        self.f = None
        self.ruta = os.path.abspath(ruta) if ruta else None
        if self.ruta:
            try:
                os.makedirs(os.path.dirname(self.ruta), exist_ok=True)
                self.f = open(self.ruta, "a", encoding="utf-8")
            except OSError as ex:
                print(f"Aviso: no se pudo crear el registro {self.ruta}: {ex}")
                self.f = None

    def __call__(self, texto: str = "", pantalla: bool = True):
        if pantalla:
            try:
                print(texto, flush=True)
            except UnicodeEncodeError:  # consola sin UTF-8
                print(texto.encode("ascii", "replace").decode("ascii"), flush=True)
        if self.f:
            try:
                self.f.write(texto + "\n")
                self.f.flush()
            except OSError:
                pass

    def cerrar(self):
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None


def _main(argv=None) -> int:
    import argparse
    import platform
    ap = argparse.ArgumentParser(
        description="Convierte planos PDF a DWG/DXF con QCAD. Acepta uno o varios PDF y "
                    "carpetas (se convierten todos los PDF de la carpeta).")
    ap.add_argument("entradas", nargs="+", metavar="PDF_O_CARPETA")
    ap.add_argument("-o", "--salida", default=None,
                    help="Carpeta de salida. Con varios PDF se crea una subcarpeta por PDF "
                         "(modo lamina).")
    ap.add_argument("--modo", choices=("lamina", "unico"), default="lamina",
                    help="lamina: un archivo por lamina. unico: un archivo por PDF con todas "
                         "las laminas lado a lado.")
    ap.add_argument("--formato", choices=("dwg", "dxf"), default="dwg")
    ap.add_argument("--paginas", default="todas", help="Laminas a convertir, p. ej. '1-5, 8'.")
    ap.add_argument("--escala", type=float, default=1.0, help="Escala 1:N del plano.")
    ap.add_argument("--unidades", choices=tuple(UNIDADES), default="mm")
    ap.add_argument("--qcad", default=QCAD_DIR_DEFECTO, help="Carpeta de instalacion de QCAD.")
    ap.add_argument("--version", default=None,
                    help="Version DWG: 2000, 2004, 2007, 2010, 2013 o 2018 (por defecto 2013).")
    ap.add_argument("--procesos", type=int, default=0,
                    help="QCAD en paralelo (0 = automatico).")
    ap.add_argument("--imagenes", action="store_true",
                    help="Importa las imagenes del PDF (quedan en la carpeta pdf_images).")
    ap.add_argument("--sin-recorte", action="store_true",
                    help="No aplica las areas de recorte del PDF.")
    ap.add_argument("--colores-originales", action="store_true",
                    help="Deja el negro fijo y los elementos blancos como vienen del PDF.")
    ap.add_argument("--saltar-existentes", action="store_true",
                    help="No repite las laminas que ya tienen su archivo terminado "
                         "(para continuar una conversion interrumpida).")
    ap.add_argument("--log", default=None, help="Guarda tambien los mensajes en este archivo.")
    a = ap.parse_args(argv)

    reg = _Registro(a.log)
    t0 = time.time()
    codigo = 0
    try:
        version = normalizar_version(a.version)
        reg("=" * 72)
        reg(f"Conversión de planos PDF a {a.formato.upper()} con QCAD - "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
        reg(f"Python {platform.python_version()}, PyMuPDF {getattr(pymupdf, 'VersionBind', '?')}, "
            f"{os.cpu_count()} núcleos, QCAD en {_limpiar_ruta(a.qcad)}", pantalla=False)
        ok_qcad, msg = diagnostico_qcad(a.qcad)
        if not ok_qcad:
            reg("ERROR: " + msg)
            return 2
        pdfs, hubo_carpeta = buscar_pdfs(a.entradas)
        for p in pdfs:
            if not os.path.isfile(p):
                reg(f"ERROR: no existe el archivo {p}")
                codigo = 1
        pdfs = [p for p in pdfs if os.path.isfile(p)]
        if not pdfs:
            reg("ERROR: no se encontraron archivos PDF en: " + ", ".join(a.entradas))
            return 2
        varios = hubo_carpeta or len(pdfs) > 1
        if a.salida:
            raiz = _limpiar_ruta(a.salida)
        elif varios:
            raiz = os.path.join(os.path.dirname(pdfs[0]), a.formato.upper())
        else:
            raiz = os.path.splitext(pdfs[0])[0] + "_" + a.formato.upper()
        op = Opciones(escala=a.escala, unidades=a.unidades,
                      negro_a_color7=not a.colores_originales,
                      omitir_blancos=not a.colores_originales,
                      recortar=not a.sin_recorte, imagenes=a.imagenes)
        ext = "." + a.formato

        # 1) lectura rapida de cada PDF
        trabajos = []
        for pdf in pdfs:
            nombre = os.path.basename(pdf)
            try:
                info = info_pdf(pdf)
                if info.get("protegido"):
                    raise ValueError("está protegido con contraseña")
                indices = interpretar_rango(a.paginas, info["paginas"])
            except Exception as ex:
                reg(f"ERROR: {nombre}: {str(ex).rstrip('.')}. Se omite.")
                codigo = 1
                continue
            base = nombre_archivo_valido(os.path.splitext(nombre)[0])
            trabajos.append({
                "pdf": pdf, "nombre": nombre, "base": base, "indices": indices,
                "digitos": max(2, len(str(info["paginas"]))),
                "destino": os.path.join(raiz, base) if (varios and a.modo == "lamina") else raiz})
        if not trabajos:
            return 2

        # 2) laminas pendientes (con --saltar-existentes se omiten las ya convertidas)
        for tr in trabajos:
            if a.modo == "lamina":
                tr["pendientes"] = [
                    i for i in tr["indices"]
                    if not (a.saltar_existentes and archivo_terminado(os.path.join(
                        tr["destino"], f"{tr['base']}_L{i + 1:0{tr['digitos']}d}{ext}")))]
            else:
                final = os.path.join(tr["destino"], tr["base"] + ext)
                ya = a.saltar_existentes and archivo_terminado(final)
                tr["pendientes"] = [] if ya else list(tr["indices"])
        total = sum(len(tr["pendientes"]) for tr in trabajos)
        omitidas = sum(len(tr["indices"]) - len(tr["pendientes"]) for tr in trabajos)
        if os.name == "nt":
            largo = max(len(os.path.join(tr["destino"], f"{tr['base']}_L{1:0{tr['digitos']}d}{ext}"))
                        for tr in trabajos)
            if largo >= 255:
                reg(f"ATENCION: las rutas de salida tienen hasta {largo} caracteres y Windows "
                    "suele limitarlas a 260. Si falla al guardar, usa una carpeta de salida más "
                    "corta (-o).")
        reg(f"{len(trabajos)} PDF, {sum(len(tr['indices']) for tr in trabajos)} láminas"
            + (f" ({omitidas} ya convertidas, se omiten)" if omitidas else "")
            + f". Salida: {raiz}")

        # 3) conversion
        hechas = 0
        t_proc = time.time()
        ancho = len(str(total))
        resumen = []
        avisos_vistos = set()
        for tr in trabajos:
            pend = tr["pendientes"]
            stats = {"ok": 0, "error": 0, "omitidas": len(tr["indices"]) - len(pend)}
            if not pend:
                reg(f"\n{tr['nombre']}: ya estaba convertido.")
                resumen.append((tr, stats, 0.0))
                continue
            reg(f"\n{tr['nombre']}: {len(pend)} láminas -> {tr['destino']}")
            os.makedirs(tr["destino"], exist_ok=True)
            # carpeta temporal local: en la de destino (p. ej. OneDrive) solo aparecen
            # los archivos terminados
            tmp = tempfile.mkdtemp(prefix="pdf_a_dwg_")
            t_pdf = time.time()
            sin_mover = []
            avisos: list = []

            def guardar(res, tr=tr, sin_mover=sin_mover):
                """Mueve el archivo terminado de la carpeta temporal a la de destino."""
                if not res.archivo or not os.path.isfile(res.archivo):
                    return
                destino = os.path.join(tr["destino"], os.path.basename(res.archivo))
                try:
                    _mover(res.archivo, destino)
                except OSError as ex:
                    sin_mover.append(res.archivo)
                    res.ok = False
                    if isinstance(ex, PermissionError):
                        motivo = "¿está abierto en otro programa?"
                    elif os.name == "nt" and len(destino) >= 255:
                        motivo = "la ruta supera el límite de 260 caracteres de Windows"
                    else:
                        motivo = str(ex)
                    res.error = f"No se pudo guardar {destino}: {motivo}"
                    return
                res.archivo = destino

            def progreso(_n, _tot, res, tr=tr, guardar=guardar, stats=stats):
                nonlocal hechas
                hechas += 1
                if a.modo == "lamina":
                    guardar(res)
                    estado = "OK" if res.ok else "ERROR"
                else:
                    estado = "leída" if res.ok else "ERROR"
                if a.modo == "lamina":
                    stats["ok" if res.ok else "error"] += 1
                linea = (f"[{hechas:>{ancho}}/{total}] {tr['base']} - lámina {res.pagina}: {estado}"
                         f" ({res.entidades} entidades, {res.segundos:.1f} s)")
                if 3 <= hechas < total:
                    resto = (total - hechas) * (time.time() - t_proc) / hechas
                    if resto >= 2:
                        linea += f" - faltan ~{_duracion(resto)}"
                reg(linea)
                if res.error:
                    reg("      " + res.error.strip().splitlines()[0][:400])

            try:
                if a.modo == "lamina":
                    convertir_por_lamina(tr["pdf"], pend, tmp, tr["base"], op, a.formato, a.qcad,
                                         version, a.procesos, progreso=progreso,
                                         digitos=tr["digitos"], avisos=avisos)
                    if op.imagenes:
                        _juntar_imagenes(tmp, tr["destino"])
                else:
                    resultados, av = convertir_unico(
                        tr["pdf"], pend, os.path.join(tmp, tr["base"] + ext), op, a.formato,
                        a.qcad, version, a.procesos, progreso=progreso,
                        etapa=lambda txt: reg("  " + txt))
                    avisos += av
                    final = ResultadoLamina(pagina=0, ok=True,
                                            archivo=next((r.archivo for r in resultados if r.ok), ""))
                    guardar(final)
                    if op.imagenes:
                        _juntar_imagenes(tmp, tr["destino"])
                    for r in resultados:
                        stats["ok" if (r.ok and final.ok) else "error"] += 1
                    if not final.ok:
                        reg("  ERROR: " + final.error)
            except KeyboardInterrupt:
                raise
            except Exception as ex:
                reg(f"ERROR al convertir {tr['nombre']}: {ex}")
                stats["error"] += len(pend) - stats["ok"] - stats["error"]
            finally:
                if sin_mover:
                    reg(f"  ATENCION: {len(sin_mover)} archivo(s) quedaron en {tmp}")
                else:
                    shutil.rmtree(tmp, ignore_errors=True)
            for av in avisos:
                if av not in avisos_vistos:
                    avisos_vistos.add(av)
                    reg("Aviso: " + av)
            if stats["error"]:
                codigo = 1
            resumen.append((tr, stats, time.time() - t_pdf))

        # 4) resumen
        reg("\n" + "=" * 72)
        reg("RESUMEN")
        tot = {"ok": 0, "error": 0, "omitidas": 0}
        for tr, st, dur in resumen:
            for k in tot:
                tot[k] += st[k]
            partes = [f"{st['ok']} {a.formato.upper()}" if a.modo == "lamina"
                      else f"{st['ok']} en 1 {a.formato.upper()}"]
            if st["omitidas"]:
                partes.append(f"{st['omitidas']} ya existían")
            if st["error"]:
                partes.append(f"{st['error']} con error")
            reg(f"  {tr['nombre']}: {len(tr['indices'])} láminas -> " + ", ".join(partes)
                + (f" ({_duracion(dur)})" if dur else ""))
        reg(f"Total: {tot['ok']} {a.formato.upper()} nuevos"
            + (f", {tot['omitidas']} ya existían" if tot["omitidas"] else "")
            + (f", {tot['error']} con error" if tot["error"] else "")
            + f". Tiempo: {_duracion(time.time() - t0)}")
        reg(f"Carpeta: {raiz}")
        return codigo
    except KeyboardInterrupt:
        reg("\nConversión interrumpida. Al volver a ejecutarla con --saltar-existentes "
            "continúa donde quedó.")
        return 130
    except ValueError as ex:
        reg(f"ERROR: {ex}")
        return 2
    finally:
        reg.cerrar()


if __name__ == "__main__":
    import sys
    sys.exit(_main())
