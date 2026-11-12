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
TZ_PERU = timezone(timedelta(hours=-5))


def cargar_estado():
    if os.path.exists(ESTADO_FILE):
        with open(ESTADO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"fechas_enviadas": []}


def guardar_estado(estado):
    estado["fechas_enviadas"] = estado["fechas_enviadas"][-30:]
    with open(ESTADO_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2)


def obtener_contexto_contenido(page):
    if "Promedio de Mercado Efectiva" in page.content():
        return page
    for frame in page.frames:
        try:
            if "Promedio de Mercado Efectiva" in frame.content():
                return frame
        except Exception:
            continue
    return page


def resaltar_y_extraer(page, es_activa=True):
    page.wait_for_timeout(2000)
    target = obtener_contexto_contenido(page)
    html = target.content()

    match_fecha = re.search(r"Promedio de Mercado Efectiva al\s+(\d{2}/\d{2}/\d{4})", html)
    if not match_fecha:
        titulo = page.title()
        texto_muestra = re.sub(r"\s+", " ", target.inner_text("body"))[:250]
        print(f"  [DEBUG] No se halló tabla. Título: '{titulo}' | Texto visible: '{texto_muestra}'")
        return None, None

    fecha_publicada = match_fecha.group(1)
    etiqueta = "TAMN" if es_activa else "TIPMN"

    valor_tasa = target.evaluate(f"""() => {{
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

    print(f"  [DEBUG] Fecha en pantalla: {fecha_publicada} | {etiqueta}: {valor_tasa}")
    return fecha_publicada, valor_tasa


def consultar_fecha_en_pagina(page, fecha_str=None):
    if not fecha_str:
        return

    target = obtener_contexto_contenido(page)
    input_fecha = target.locator("input:not([type='hidden']):not([type='submit']):not([type='button'])").first
    input_fecha.wait_for(state="visible", timeout=15000)
    input_fecha.fill("")
    input_fecha.fill(fecha_str)

    btn_consultar = target.locator("input[value*='Consultar'], button:has-text('Consultar')").first
    btn_consultar.click()
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)


def enviar_correo(fecha_str, tamn, tipmn, img_activa_path, img_pasiva_path):
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    email_to = os.environ["EMAIL_TO"]
    email_cc = os.environ.get("EMAIL_CC", "")

    print(f"  [EMAIL] Enviando reporte de {fecha_str} a {email_to} (CC: {email_cc})...")

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
    print(f"  [OK] Correo enviado exitosamente para {fecha_str}")


def main():
    estado = cargar_estado()
    fechas_enviadas = set(estado.get("fechas_enviadas", []))
    print(f"Fechas ya registradas en estado.json: {sorted(list(fechas_enviadas))}")

    hoy_peru = datetime.now(TZ_PERU).date()
    fechas_a_probar = [None] + [
        (hoy_peru + timedelta(days=i)).strftime("%d/%m/%Y") for i in range(0, 5)
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            locale="es-PE"
        )
        page_activa = context.new_page()
        page_pasiva = context.new_page()

        for fecha_objetivo in fechas_a_probar:
            if fecha_objetivo and fecha_objetivo in fechas_enviadas:
                print(f"Saltando {fecha_objetivo} (ya enviada previamente).")
                continue

            print(f"Consultando SBS (objetivo: {fecha_objetivo or 'Por defecto'})...")
            try:
                page_activa.goto(URL_ACTIVA, wait_until="domcontentloaded", timeout=60000)
                page_activa.wait_for_timeout(2500)
                consultar_fecha_en_pagina(page_activa, fecha_objetivo)
                fecha_act, tamn = resaltar_y_extraer(page_activa, es_activa=True)
            except Exception as e:
                print(f"  [ERROR] Al consultar activa ({fecha_objetivo}): {e}")
                continue

            if not fecha_act or not tamn:
                continue

            if fecha_act in fechas_enviadas:
                print(f"  [INFO] La fecha {fecha_act} ya fue enviada anteriormente. No se envía correo.")
                continue

            try:
                page_pasiva.goto(URL_PASIVA, wait_until="domcontentloaded", timeout=60000)
                page_pasiva.wait_for_timeout(2500)
                consultar_fecha_en_pagina(page_pasiva, fecha_act)
                fecha_pas, tipmn = resaltar_y_extraer(page_pasiva, es_activa=False)
            except Exception as e:
                print(f"  [ERROR] Al consultar pasiva ({fecha_act}): {e}")
                continue

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
            else:
                print(f"  [INFO] Tasa pasiva aún no coincide con activa ({fecha_act} vs {fecha_pas}).")

        browser.close()


if __name__ == "__main__":
    main()
