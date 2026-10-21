# -*- coding: utf-8 -*-
"""
app.py - Interfaz grafica para convertir planos PDF a DWG / DXF con QCAD.

Ejecutar:      streamlit run app.py        (o doble clic en iniciar_app.bat)
Requisitos:    pip install -r requirements.txt   (streamlit, pymupdf)
               QCAD Professional (o su version de prueba), por defecto en
               C:\\Program Files\\QCAD

QCAD hace la conversion con su importador de PDF (el mismo de pdf2dwg); el codigo
esta en conversor.py (misma carpeta). La conversion corre en segundo plano: se puede
seguir usando la pagina mientras avanza y se puede cancelar.
"""
import hashlib
import os
import shutil
import tempfile
import threading
import time
import zipfile
from collections import Counter

import streamlit as st

import conversor as cv

MODO_LAMINA = "Un DWG por lámina (ZIP)"
MODO_UNICO = "Un solo DWG con todas las láminas"
LIMITE_DESCARGA = 1536 * 1024 * 1024  # sobre esto solo se guarda en carpeta (no en memoria)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _carpeta_trabajo() -> str:
    """Carpeta temporal de la app; borra restos de sesiones de mas de 24 h."""
    raiz = os.path.join(tempfile.gettempdir(), "pdf_a_dwg")
    os.makedirs(raiz, exist_ok=True)
    ahora = time.time()
    for nombre in os.listdir(raiz):
        ruta = os.path.join(raiz, nombre)
        try:
            if ahora - os.path.getmtime(ruta) > 86400:
                shutil.rmtree(ruta, ignore_errors=True)
        except OSError:
            pass
    return raiz


def _fmt_tiempo(seg: float) -> str:
    seg = int(round(seg))
    if seg < 60:
        return f"{seg} s"
    m, s = divmod(seg, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


def _fmt_bytes(n: float) -> str:
    for unidad in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidad == "GB":
            return f"{n:.0f} {unidad}" if unidad == "B" else f"{n:.1f} {unidad}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _ruta_limpia(texto: str) -> str:
    """Quita comillas y espacios (p.ej. al usar 'Copiar como ruta' del Explorador)."""
    return (texto or "").strip().strip('"').strip()


@st.cache_data(show_spinner=False, max_entries=8)
def _info(clave: str, ruta: str, password: str) -> dict:
    return cv.info_pdf(ruta, password)


@st.cache_data(show_spinner=False, max_entries=64)
def _miniatura(clave: str, ruta: str, indice: int, password: str) -> bytes:
    with cv.BLOQUEO_PYMUPDF:
        doc = cv.pymupdf.open(ruta)
        try:
            if doc.needs_pass:
                doc.authenticate(password)
            pg = doc[indice]
            zoom = min(1400.0 / max(pg.rect.width, pg.rect.height), 3.0)
            pix = pg.get_pixmap(matrix=cv.pymupdf.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()


def _es_pdf(ruta: str) -> bool:
    try:
        with open(ruta, "rb") as f:
            return b"%PDF" in f.read(1024)
    except OSError:
        return False


def _preparar_entrada(subido, ruta_local: str):
    """Devuelve (clave, ruta_pdf, nombre) del PDF a convertir, o (None, None, error)."""
    raiz = _carpeta_trabajo()
    ruta_local = _ruta_limpia(ruta_local)
    if ruta_local:
        if not os.path.isfile(ruta_local):
            return None, None, "No existe el archivo indicado."
        if not ruta_local.lower().endswith(".pdf") or not _es_pdf(ruta_local):
            return None, None, "Solo se pueden convertir archivos PDF."
        est = os.stat(ruta_local)
        clave = hashlib.md5(f"{ruta_local}|{est.st_size}|{est.st_mtime}".encode()).hexdigest()
        return clave, ruta_local, os.path.basename(ruta_local)
    if subido is None:
        return None, None, None
    if not subido.name.lower().endswith(".pdf"):
        return None, None, "Solo se pueden convertir archivos PDF."
    id_archivo = getattr(subido, "file_id", None) or f"{subido.name}|{subido.size}"
    clave = hashlib.md5(str(id_archivo).encode()).hexdigest()
    previo = st.session_state.get("entrada")
    if previo and previo["clave"] == clave and os.path.isfile(previo["ruta"]):
        return clave, previo["ruta"], subido.name
    trabajo = st.session_state.get("trabajo")
    if previo and not (trabajo and not trabajo.terminado):
        # se reemplaza el PDF anterior (salvo que se este convirtiendo)
        shutil.rmtree(os.path.dirname(previo["ruta"]), ignore_errors=True)
    carpeta = tempfile.mkdtemp(prefix="sesion_", dir=raiz)
    ruta = os.path.join(carpeta, "entrada.pdf")
    with open(ruta, "wb") as f:
        f.write(subido.getbuffer())
    if not _es_pdf(ruta):
        shutil.rmtree(carpeta, ignore_errors=True)
        return None, None, "El archivo no es un PDF válido."
    st.session_state["entrada"] = {"clave": clave, "ruta": ruta}
    return clave, ruta, subido.name


def _imagenes_de(carpeta: str) -> list:
    """Imagenes que dejo QCAD en carpeta/pdf_images (los DWG las enlazan ahi)."""
    dir_img = os.path.join(carpeta, "pdf_images")
    if not os.path.isdir(dir_img):
        return []
    return [os.path.join(dir_img, n) for n in sorted(os.listdir(dir_img))
            if os.path.isfile(os.path.join(dir_img, n))]


def _empaquetar(archivos, imagenes, carpeta, nombre_zip, formato):
    """ZIP con los archivos generados (y la carpeta pdf_images si hay imagenes)."""
    ruta_zip = os.path.join(carpeta, nombre_zip)
    compresion = zipfile.ZIP_STORED if formato == "dwg" else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(ruta_zip, "w", compression=compresion, allowZip64=True) as z:
        for a in archivos:
            z.write(a, os.path.basename(a))
        for a in imagenes:
            z.write(a, "pdf_images/" + os.path.basename(a))
    return ruta_zip


def _guardar_en_carpeta(archivos, imagenes, destino):
    os.makedirs(destino, exist_ok=True)
    copiados = []
    for a in archivos:
        if a and os.path.isfile(a):
            shutil.copy2(a, os.path.join(destino, os.path.basename(a)))
            copiados.append(os.path.basename(a))
    if imagenes:
        dir_img = os.path.join(destino, "pdf_images")
        os.makedirs(dir_img, exist_ok=True)
        for a in imagenes:
            shutil.copy2(a, os.path.join(dir_img, os.path.basename(a)))
    return copiados


# ---------------------------------------------------------------------------
# Conversion en segundo plano
# ---------------------------------------------------------------------------
@st.cache_resource
def _registro_trabajos() -> dict:
    """Conversiones en curso en este servidor (de todas las pestañas)."""
    return {}


class Trabajo:
    """Estado de una conversion que corre en un hilo aparte."""

    def __init__(self, clave: str, total: int):
        self.clave = clave
        self.total = total
        self.hechas = 0
        self.texto = "Abriendo QCAD…"
        self.inicio = time.time()
        self.terminado = False
        self.resultado = None
        self.error = None
        self.cancelar = threading.Event()
        self.nombre = ""


def _convertir(trabajo, ruta_pdf, nombre_pdf, password, indices, modo, formato, op, qcad_dir,
               version, procesos, destino, total_paginas):
    base = cv.nombre_archivo_valido(os.path.splitext(nombre_pdf)[0])[:60]
    avisos = []
    carpeta = tempfile.mkdtemp(prefix="salida_", dir=_carpeta_trabajo())
    t0 = time.time()
    trabajo.texto = "Abriendo QCAD… (la versión de prueba espera 15 s al abrirse)"

    def progreso(n, tot, res):
        trabajo.hechas = n
        resto = (time.time() - t0) / n * (tot - n)
        texto = f"Lámina {res.pagina} lista ({n}/{tot})"
        if n < tot:
            texto += f" · faltan ~{_fmt_tiempo(resto)}"
        trabajo.texto = texto

    def etapa(texto):
        trabajo.texto = texto

    cancelar = trabajo.cancelar.is_set
    try:
        if modo == MODO_LAMINA:
            # numeracion segun el total de laminas del PDF: _L005 siempre igual
            resultados = cv.convertir_por_lamina(ruta_pdf, indices, carpeta, base, op, formato,
                                                 qcad_dir, version, procesos, password,
                                                 progreso, cancelar,
                                                 digitos=max(2, len(str(total_paginas))),
                                                 avisos=avisos)
            if cancelar():
                avisos.append(f"Conversión cancelada: se convirtieron "
                              f"{sum(1 for r in resultados if r.ok)} de {len(indices)} láminas.")
            archivos = [r.archivo for r in resultados if r.ok and os.path.isfile(r.archivo)]
            if not archivos:
                if cancelar():
                    raise cv.Cancelado("Conversión cancelada.")
                motivo = next((r.error for r in resultados if r.error), "")
                raise RuntimeError("No se generó ningún archivo." + (f" {motivo}" if motivo else ""))
            imagenes = _imagenes_de(carpeta)
            if len(archivos) == 1 and not imagenes:
                final = archivos[0]
            else:
                trabajo.texto = "Comprimiendo archivos…"
                final = _empaquetar(archivos, imagenes, carpeta, f"{base}_{formato.upper()}.zip",
                                    formato)
        else:
            salida = os.path.join(carpeta, f"{base}.{formato}")
            resultados, av = cv.convertir_unico(ruta_pdf, indices, salida, op, formato, qcad_dir,
                                                version, procesos, password, progreso, cancelar,
                                                etapa)
            avisos += av
            archivos = [salida] if os.path.isfile(salida) else []
            if not archivos:
                raise RuntimeError("No se generó el archivo de salida.")
            imagenes = _imagenes_de(carpeta)
            final = salida
            if imagenes:
                trabajo.texto = "Comprimiendo archivos…"
                final = _empaquetar(archivos, imagenes, carpeta, f"{base}_{formato.upper()}.zip",
                                    formato)
        guardados = []
        if destino:
            trabajo.texto = "Guardando en la carpeta…"
            try:
                guardados = _guardar_en_carpeta(archivos, imagenes, destino)
            except OSError as ex:
                avisos.append(f"No se pudo guardar en la carpeta indicada: {ex}")
        tamano = os.path.getsize(final)
        datos = None
        if tamano <= LIMITE_DESCARGA or not guardados:
            with open(final, "rb") as f:
                datos = f.read()
        return {
            "clave": trabajo.clave,
            "datos": datos,
            "tamano": tamano,
            "nombre": os.path.basename(final),
            "mime": "application/zip" if final.endswith(".zip") else "application/octet-stream",
            "resultados": resultados,
            "avisos": avisos,
            "segundos": time.time() - trabajo.inicio,
            "modo": modo,
            "destino": destino if guardados else "",
            "guardados": guardados,
            "imagenes": len(imagenes),
        }
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


def _hilo_conversion(trabajo, parametros, registro):
    try:
        trabajo.resultado = _convertir(trabajo, **parametros)
    except cv.Cancelado:
        trabajo.error = "Conversión cancelada."
    except Exception as ex:
        trabajo.error = f"La conversión falló: {ex}"
    finally:
        trabajo.terminado = True
        registro.pop(id(trabajo), None)


def _recoger_trabajo(trabajo):
    """Pasa el resultado (o el error) de un trabajo terminado a la sesion."""
    st.session_state["trabajo"] = None
    if trabajo.error:
        st.session_state["error_trabajo"] = (trabajo.clave, trabajo.error)
    else:
        st.session_state["resultado"] = trabajo.resultado


def _seguir_trabajo(trabajo):
    """Muestra el avance de la conversion en curso hasta que termine."""
    st.info("Convirtiendo con QCAD… puedes seguir usando la página; la conversión continúa.")
    barra = st.progress(0.0, text=trabajo.texto)
    if st.button("Cancelar conversión"):
        trabajo.cancelar.set()
    while not trabajo.terminado:
        avance = min(trabajo.hechas / max(1, trabajo.total), 1.0)
        texto = trabajo.texto
        if trabajo.cancelar.is_set():
            texto = "Cancelando…"
        barra.progress(avance, text=f"{texto} · {_fmt_tiempo(time.time() - trabajo.inicio)}")
        time.sleep(0.4)
    _recoger_trabajo(trabajo)
    st.rerun()


def _mostrar_resultado(r):
    res = r["resultados"]
    ok = sum(1 for x in res if x.ok)
    fallidas = [x for x in res if not x.ok]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Láminas convertidas", f"{ok} de {len(res)}")
    c2.metric("Entidades", f"{sum(x.entidades for x in res):,}".replace(",", "."))
    c3.metric("Tiempo", _fmt_tiempo(r["segundos"]))
    c4.metric("Archivo", _fmt_bytes(r["tamano"]))

    if r["datos"] is not None:
        st.download_button(f"⬇️ Descargar {r['nombre']}", r["datos"], file_name=r["nombre"],
                           mime=r["mime"], type="primary")
    if r["destino"]:
        st.success(f"Guardado en: {r['destino']} ({len(r['guardados'])} archivo(s)).")
    if r.get("imagenes"):
        st.info(f"Se importaron {r['imagenes']} imagen(es): van en la carpeta pdf_images, "
                "que debe quedar junto a los DWG.")
    for a in r["avisos"]:
        st.warning(a)
    if fallidas:
        st.error("Láminas con error: " + "; ".join(f"{x.pagina} ({x.error})" for x in fallidas))
    vacias = [x.pagina for x in res if x.ok and x.entidades == 0]
    if vacias:
        st.warning("Láminas sin contenido vectorial (posiblemente escaneadas, quedan vacías): "
                   + ", ".join(map(str, vacias)))

    filas = [{"Lámina": x.pagina, "Estado": "✅" if x.ok else "❌", "Entidades": x.entidades,
              "Segundos": x.segundos, "Detalle": x.error} for x in res]
    with st.expander("Detalle por lámina", expanded=len(res) <= 12):
        st.dataframe(filas, hide_index=True)


# ---------------------------------------------------------------------------
# Interfaz
# ---------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="PDF a DWG", page_icon="📐", layout="wide")
    st.title("📐 Conversor de planos PDF → DWG con QCAD")
    st.caption("QCAD lee cada lámina del PDF con su importador (el mismo de pdf2dwg) y la "
               "guarda como DWG o DXF. Funciona con PDF de muchas láminas.")
    trabajo = st.session_state.get("trabajo")
    if trabajo is not None and trabajo.terminado:
        _recoger_trabajo(trabajo)
        trabajo = None
    en_curso = trabajo is not None
    registro = _registro_trabajos()

    # ---- barra lateral: configuracion -----------------------------------------
    with st.sidebar:
        st.header("QCAD")
        qcad_dir = _ruta_limpia(st.text_input("Carpeta de QCAD", value=cv.QCAD_DIR_DEFECTO))
        qcad_ok, qcad_msg = cv.diagnostico_qcad(qcad_dir)
        if qcad_ok:
            st.success(qcad_msg)
        else:
            st.error(qcad_msg)
        version_txt = st.selectbox("Versión del DWG", list(cv.VERSIONES_DWG), index=0,
                                   help="AutoCAD 2013 es la que usa QCAD por defecto y la abre "
                                        "cualquier AutoCAD desde 2013.")
        version = cv.VERSIONES_DWG[version_txt]
        if qcad_ok and st.button("Probar QCAD", disabled=en_curso):
            cv._CACHE_PRUEBA_QCAD.pop((cv._limpiar_ruta(qcad_dir), version), None)
            with st.spinner("Convirtiendo un PDF de prueba con QCAD…"):
                try:
                    _v, av = cv.probar_qcad(qcad_dir, version)
                    st.success("QCAD convierte correctamente.")
                    for a in av:
                        st.info(a)
                except Exception as ex:
                    st.error(f"QCAD no funcionó: {ex}")
        nucleos = os.cpu_count() or 1
        procesos = st.slider("QCAD en paralelo", 1, max(2, min(8, nucleos)),
                             max(1, min(4, nucleos)),
                             help="Cuántos QCAD convierten láminas a la vez. Más rápido, pero "
                                  "cada uno usa memoria.")
        otros = [t for t in list(registro.values()) if t is not trabajo and not t.terminado]
        if otros:
            st.warning(f"Hay {len(otros)} conversión(es) en curso desde otra pestaña o "
                       "una página recargada: " + ", ".join(t.nombre for t in otros))
            if st.button("Cancelar esas conversiones"):
                for t in otros:
                    t.cancelar.set()
        st.divider()
        st.caption("La versión de prueba de QCAD espera 15 s cada vez que se abre: la app "
                   "convierte muchas láminas por cada vez que lo abre.")

    # ---- entrada -------------------------------------------------------------------
    subido = st.file_uploader("Plano en PDF", type=["pdf"], accept_multiple_files=False)
    with st.expander("¿PDF muy grande? Usar un archivo de este equipo por su ruta"):
        ruta_local = st.text_input("Ruta del PDF", value="",
                                   placeholder=r"C:\Planos\Subestacion.pdf",
                                   help="Evita el límite de tamaño de subida del navegador.")

    clave, ruta_pdf, nombre = _preparar_entrada(subido, ruta_local)
    if clave is None:
        if nombre:
            st.error(nombre)
        else:
            st.info("Sube un plano en PDF para comenzar.")
        if en_curso:
            _seguir_trabajo(trabajo)
        return

    password = ""
    info = _info(clave, ruta_pdf, "")
    if info["protegido"]:
        password = st.text_input("Este PDF tiene contraseña", type="password")
        if not password:
            return
        info = _info(clave, ruta_pdf, password)
        if info["protegido"]:
            st.error("Contraseña incorrecta.")
            return
    total = info["paginas"]
    if total == 0:
        st.error("El PDF no tiene páginas.")
        return

    # ---- resumen del PDF -------------------------------------------------------------
    papeles = Counter(d["papel"] for d in info["detalle"])
    c1, c2 = st.columns([1, 3])
    c1.metric("Láminas en el PDF", total)
    c2.markdown("**Tamaños:** " + " · ".join(f"{n} × {p}" for p, n in papeles.most_common(6)))
    with st.expander("Vista previa"):
        n_prev = st.number_input("Lámina", min_value=1, max_value=total, value=1, step=1) \
            if total > 1 else 1
        d = info["detalle"][int(n_prev) - 1]
        st.caption(f"Lámina {d['pagina']}: {d['papel']} ({d['ancho_mm']} × {d['alto_mm']} mm)")
        try:
            st.image(_miniatura(clave, ruta_pdf, int(n_prev) - 1, password))
        except Exception as ex:
            st.warning(f"No se pudo generar la vista previa: {ex}")

    # ---- opciones --------------------------------------------------------------------
    with st.form("opciones"):
        c1, c2 = st.columns(2)
        modo = c1.radio("Resultado", [MODO_LAMINA, MODO_UNICO], index=0)
        formato = c2.radio("Formato", ["DWG", "DXF"], index=0, horizontal=True)
        rango = c2.text_input("Láminas a convertir", value="todas",
                              help="Ejemplos: todas · 1-5 · 1, 3, 8-10")
        c1, c2 = st.columns(2)
        escala = c1.number_input("Escala del plano  1 :", min_value=0.0001, value=1.0,
                                 step=1.0, format="%g",
                                 help="1 = tamaño real del papel. Ej.: plano a 1:100 → escribe 100 "
                                      "para obtener las medidas reales.")
        unidades = c2.selectbox("Unidades del dibujo", list(cv.UNIDADES), index=0)
        with st.expander("Opciones avanzadas"):
            a1, a2 = st.columns(2)
            negro = a1.checkbox("Negro → color 7 (se ve en fondo claro y oscuro)", value=True,
                                help="QCAD importa el negro como color fijo; en el fondo oscuro "
                                     "de AutoCAD casi no se ve.")
            blancos = a1.checkbox("Omitir líneas y rellenos blancos (invisibles en papel)",
                                  value=True)
            recortar = a2.checkbox("Recortar a las áreas de recorte del PDF", value=True)
            imagenes = a2.checkbox("Incluir imágenes (logos, fotos)", value=False,
                                   help="QCAD las guarda como archivos en una carpeta pdf_images "
                                        "que debe quedar junto a los DWG.")
            st.markdown("**Modo “un solo DWG”**")
            b1, b2 = st.columns(2)
            marco = b1.checkbox("Dibujar marco y número de cada lámina", value=True)
            por_fila = b2.number_input("Láminas por fila (0 = todas en una fila)",
                                       min_value=0, max_value=200, value=0, step=1)
            destino = st.text_input("Guardar también en esta carpeta (opcional)", value="",
                                    placeholder=r"C:\Planos\DWG")
        enviar = st.form_submit_button("Convertir", type="primary",
                                       disabled=en_curso or not qcad_ok)

    if enviar and not en_curso:
        try:
            indices = cv.interpretar_rango(rango, total)
        except ValueError as ex:
            st.error(str(ex))
            return
        op = cv.Opciones(escala=float(escala), unidades=unidades, negro_a_color7=negro,
                         omitir_blancos=blancos, recortar=recortar, imagenes=imagenes,
                         marco_laminas=marco, laminas_por_fila=int(por_fila))
        parametros = dict(ruta_pdf=ruta_pdf, nombre_pdf=nombre, password=password,
                          indices=indices, modo=modo, formato=formato.lower(), op=op,
                          qcad_dir=qcad_dir, version=version, procesos=procesos,
                          destino=_ruta_limpia(destino), total_paginas=total)
        trabajo = Trabajo(clave, len(indices))
        trabajo.nombre = nombre
        st.session_state["trabajo"] = trabajo
        st.session_state.pop("resultado", None)
        st.session_state.pop("error_trabajo", None)
        registro[id(trabajo)] = trabajo
        threading.Thread(target=_hilo_conversion, args=(trabajo, parametros, registro),
                         daemon=True).start()
        en_curso = True

    if en_curso:
        _seguir_trabajo(st.session_state["trabajo"])
        return

    error = st.session_state.get("error_trabajo")
    if error and error[0] == clave:
        st.error(error[1])
    resultado = st.session_state.get("resultado")
    if resultado and resultado.get("clave") == clave:
        _mostrar_resultado(resultado)


# La guarda evita que la interfaz se ejecute si otro programa importa este archivo.
if __name__ == "__main__":
    main()
