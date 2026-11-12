import os
import re
import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

URL_ACTIVA = "https://www.sbs.gob.pe/app/pp/EstadisticasSAEEPortal/Paginas/TIActivaMercado.aspx?tip=B"
URL_PASIVA = "https://www.sbs.gob.pe/app/pp/EstadisticasSAEEPortal/Paginas/TIPasivaMercado.aspx?tip=B"
ESTADO_FILE = "estado.json"

# Hora actual en Perú (UTC-5)
TZ_PERU = timezone(timedelta(hours=-5))


def cargar_estado():
    if os.path.exists(ESTADO_FILE):
        with open(ESTADO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"fechas_enviadas": []}


def guardar_estado(estado):
    # Mantener solo las últimas 30 fechas para que el archivo no crezca innecesariamente
    estado["fechas_enviadas"] = estado["fechas_enviadas"][-30:]
    with open(ESTADO_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2)


def resaltar_y_extraer(page, es_activa=True):
    """
    Extrae la fecha real del título de la tabla, obtiene el valor de TAMN o TIPMN,
    y resalta la celda en verde claro antes de tomar la captura de pantalla.
    """
    page.wait_for_timeout(1500)
    html = page.content()

    # Verificar la fecha real mostrada en el encabezado de la tabla: "... al DD/MM/AAAA"
    match_fecha = re.search(r"Promedio de Mercado Efectiva al\s+(\d{2}/\d{2}/\d{4})", html)
    if not match_fecha:
        return None, None

    fecha_publicada = match_fecha.group(1)
    etiqueta = "TAMN" if es_activa else "TIPMN"

    # Buscar la fila que contiene Moneda Nacional (TAMN) o (TIPMN) y resaltar su tasa
    valor_tasa = page.evaluate(f"""() => {{
        const rows = Array.from(document.querySelectorAll('tr'));
        for (const row of rows) {{
            if (row.innerText.includes('Moneda Nacional') && row.innerText.includes('{etiqueta}')) {{
                const cells = Array.from(row.querySelectorAll('td'));
                for (const cell of cells) {{
                    const txt = cell.innerText.trim();
                    if (txt.endsWith('%')) {{
                        cell.style.backgroundColor = '#a8e6cf';
                        cell.style.fontWeight = 'bold';
                        cell.style.border = '2px solid #2e7d32';
                        return txt;
                    }}
                }}
            }}
        }}
        return null;
    }}""")

    return fecha_publicada, valor_tasa


def consultar_fecha_en_pagina(page, fecha_str=None):
    """Si se pasa fecha_str (DD/MM/YYYY), la ingresa en el formulario y presiona Consultar."""
    if fecha_str:
        input_fecha = page.locator("input[type='text']").first
        input_fecha.fill("")
        input_fecha.fill(fecha_str)
        page.locator("input[value='Consultar'], button:has-text('Consultar')").first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)


def enviar_correo(fecha_str, tamn, tipmn, img_activa_path, img_pasiva_path):
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    email_to = os.environ["EMAIL_TO"]
    email_cc = os.environ.get("EMAIL_CC", "")

    msg = EmailMessage()
    msg["Subject"] = f"Tasas SBS ({fecha_str}) - TAMN: {tamn} | TIPMN: {tipmn}"
    msg["From"] = smtp_user
    msg["To"] = email_to
    if email_cc:
        msg["Cc"] = email_cc

    cuerpo_html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; color: #222;">
        <h2 style="color: #003366;">Reporte Diario de Tasas de Interés SBS</h2>
        <p>Se detectó la publicación oficial correspondiente al <strong>{fecha_str}</strong>:</p>
        <table style="border-collapse: collapse; width: 420px; margin: 16px 0;">
          <thead>
            <tr style="background-color: #003366; color: white;">
              <th style="padding: 10px; border: 1px solid #ccc; text-align: left;">Indicador</th>
              <th style="padding: 10px; border: 1px solid #ccc; text-align: right;">Tasa Anual</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td style="padding: 10px; border: 1px solid #ccc;"><strong>TAMN</strong> (Activa Moneda Nacional)</td>
              <td style="padding: 10px; border: 1px solid #ccc; text-align: right; background-color: #e8f5e9; font-weight: bold;">{tamn}</td>
            </tr>
            <tr>
              <td style="padding: 10px; border: 1px solid #ccc;"><strong>TIPMN</strong> (Pasiva Moneda Nacional)</td>
              <td style="padding: 10px; border: 1px solid #ccc; text-align: right; background-color: #e8f5e9; font-weight: bold;">{tipmn}</td>
            </tr>
          </tbody>
        </table>
        <p style="font-size: 13px; color: #555;">Se adjuntan las capturas de pantalla de ambos portales oficiales de la SBS como sustento.</p>
      </body>
    </html>
    """
    msg.set_content(f"Tasas SBS al {fecha_str} -> TAMN: {tamn} | TIPMN: {tipmn}")
    msg.add_alternative(cuerpo_html, subtype="html")

    fecha_slug = fecha_str.replace("/", "-")
    for path, nombre_adjunto in [
        (img_activa_path, f"SBS_Tasa_Activa_{fecha_slug}.png"),
        (img_pasiva_path, f"SBS_Tasa_Pasiva_{fecha_slug}.png"),
    ]:
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="image",
                subtype="png",
                filename=nombre_adjunto,
            )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    print(f"[OK] Correo enviado para la fecha {fecha_str}")


def main():
    estado = cargar_estado()
    fechas_enviadas = set(estado.get("fechas_enviadas", []))

    hoy_peru = datetime.now(TZ_PERU).date()
    # Revisar primero la carga por defecto (None) y luego hoy hasta +4 días (fines de semana / feriados)
    fechas_a_probar = [None] + [
        (hoy_peru + timedelta(days=i)).strftime("%d/%m/%Y") for i in range(0, 5)
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page_activa = context.new_page()
        page_pasiva = context.new_page()

        for fecha_objetivo in fechas_a_probar:
            if fecha_objetivo and fecha_objetivo in fechas_enviadas:
                continue

            print(f"Consultando SBS (objetivo: {fecha_objetivo or 'Por defecto'})...")
            page_activa.goto(URL_ACTIVA, wait_until="networkidle", timeout=60000)
            consultar_fecha_en_pagina(page_activa, fecha_objetivo)
            fecha_act, tamn = resaltar_y_extraer(page_activa, es_activa=True)

            if not fecha_act or not tamn or fecha_act in fechas_enviadas:
                continue

            # Verificar que la tasa pasiva también esté publicada para esa misma fecha
            page_pasiva.goto(URL_PASIVA, wait_until="networkidle", timeout=60000)
            consultar_fecha_en_pagina(page_pasiva, fecha_act)
            fecha_pas, tipmn = resaltar_y_extraer(page_pasiva, es_activa=False)

            if fecha_pas == fecha_act and tipmn:
                img_act = "/tmp/activa.png"
                img_pas = "/tmp/pasiva.png"
                page_activa.screenshot(path=img_act, full_page=True)
                page_pasiva.screenshot(path=img_pas, full_page=True)

                enviar_correo(fecha_act, tamn, tipmn, img_act, img_pas)
                fechas_enviadas.add(fecha_act)
                estado["fechas_enviadas"] = sorted(
                    list(fechas_enviadas),
                    key=lambda d: datetime.strptime(d, "%d/%m/%Y")
                )
                guardar_estado(estado)

        browser.close()


if __name__ == "__main__":
    main()
