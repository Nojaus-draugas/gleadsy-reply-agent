import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config

logger = logging.getLogger(__name__)


def send_email_notification(subject: str, body_html: str):
    """Send email notification via Gmail SMTP."""
    if not config.NOTIFY_EMAIL or not config.GMAIL_APP_PASSWORD:
        logger.debug("Email notification skipped (not configured)")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = config.NOTIFY_EMAIL
        msg["To"] = config.NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body_html, "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(config.NOTIFY_EMAIL, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)

        logger.info(f"Email notification sent: {subject}")
    except Exception as e:
        logger.error(f"Email notification failed: {e}")


def notify_escalation_email(lead_email: str, client_id: str, classification: str,
                            confidence: float, original_message: str, reasoning: str):
    """Send escalation email for UNCERTAIN or important replies."""
    subject = f"⚠️ Gleadsy: reikia peržiūros - {lead_email}"
    body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #e65100;">⚠️ Reply reikia tavo peržiūros</h2>
        <table style="width: 100%; border-collapse: collapse;">
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Lead:</td>
                <td style="padding: 8px;">{lead_email}</td></tr>
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Klientas:</td>
                <td style="padding: 8px;">{client_id}</td></tr>
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Klasifikacija:</td>
                <td style="padding: 8px;">{classification} ({confidence:.0%})</td></tr>
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Priežastis:</td>
                <td style="padding: 8px;">{reasoning}</td></tr>
        </table>
        <h3 style="color: #333; margin-top: 20px;">Originali žinutė:</h3>
        <div style="background: #f5f5f5; padding: 15px; border-radius: 8px; border-left: 4px solid #e65100; white-space: pre-wrap;">{original_message}</div>
        <p style="margin-top: 20px; color: #888; font-size: 12px;">
            <a href="{config.DASHBOARD_BASE_URL}/replies">Atidaryti dashboard</a>
        </p>
    </div>
    """
    send_email_notification(subject, body)


def notify_interested_email(lead_email: str, client_id: str, original_message: str, generated_reply: str):
    """Notify about INTERESTED lead with draft reply."""
    subject = f"🔥 Gleadsy: INTERESTED lead - {lead_email}"
    body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #2e7d32;">🔥 Naujas suinteresuotas lead!</h2>
        <table style="width: 100%; border-collapse: collapse;">
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Lead:</td>
                <td style="padding: 8px;">{lead_email}</td></tr>
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Klientas:</td>
                <td style="padding: 8px;">{client_id}</td></tr>
        </table>
        <h3 style="color: #333; margin-top: 20px;">Lead parašė:</h3>
        <div style="background: #f5f5f5; padding: 15px; border-radius: 8px; border-left: 4px solid #2e7d32; white-space: pre-wrap;">{original_message}</div>
        <h3 style="color: #333; margin-top: 20px;">Sugeneruotas atsakymas (draft):</h3>
        <div style="background: #e8f5e9; padding: 15px; border-radius: 8px; border-left: 4px solid #4caf50; white-space: pre-wrap;">{generated_reply}</div>
        <p style="margin-top: 20px; color: #888; font-size: 12px;">
            <a href="{config.DASHBOARD_BASE_URL}/replies">Atidaryti dashboard</a>
        </p>
    </div>
    """
    send_email_notification(subject, body)


def notify_order_placed_email(lead_email: str, client_id: str, campaign_name: str,
                              original_message: str, generated_reply: str, confidence: float,
                              interaction_id: int | None = None):
    """KRITINE notifikacija - prospect'as patvirtino uzsakyma. Visada siunciama email'u."""
    subject = f"🚨🛒 Gleadsy: UZSAKYMAS patvirtintas - {lead_email}"
    link = f"{config.DASHBOARD_BASE_URL}/pending"
    if interaction_id:
        link = f"{config.DASHBOARD_BASE_URL}/pending#draft-{interaction_id}"
    body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: #c62828; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
            <h2 style="margin: 0;">🚨🛒 UŽSAKYMAS PATVIRTINTAS</h2>
            <p style="margin: 5px 0 0 0; opacity: 0.9;">Prospektas įsipareigojo pirkti - reikia tavo veiksmų</p>
        </div>
        <div style="background: #fff; padding: 20px; border: 1px solid #eee; border-top: none;">
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px; font-weight: bold; color: #555;">Lead:</td>
                    <td style="padding: 8px;"><strong>{lead_email}</strong></td></tr>
                <tr><td style="padding: 8px; font-weight: bold; color: #555;">Klientas:</td>
                    <td style="padding: 8px;">{client_id}</td></tr>
                <tr><td style="padding: 8px; font-weight: bold; color: #555;">Kampanija:</td>
                    <td style="padding: 8px;">{campaign_name}</td></tr>
                <tr><td style="padding: 8px; font-weight: bold; color: #555;">Confidence:</td>
                    <td style="padding: 8px;">{confidence:.0%}</td></tr>
            </table>
            <h3 style="color: #c62828; margin-top: 20px;">Lead parašė:</h3>
            <div style="background: #fff3e0; padding: 15px; border-radius: 8px; border-left: 4px solid #e65100; white-space: pre-wrap;">{original_message}</div>
            <h3 style="color: #333; margin-top: 20px;">Sugeneruotas draft atsakymas:</h3>
            <div style="background: #f5f5f5; padding: 15px; border-radius: 8px; border-left: 4px solid #666; white-space: pre-wrap;">{generated_reply}</div>
            <div style="margin-top: 25px; padding: 15px; background: #ffebee; border-radius: 8px;">
                <strong>⚠️ DRAFT NESIUNCIAMAS AUTOMATISKAI</strong><br>
                Laukia tavo approval - turi peržiūrėti, pakoreguoti ir tik tada patvirtinti siuntimą.<br>
                Paprastai užsakymams reikia: patvirtinti detales, išsiųsti sąskaitą, sutarti pristatymą.
            </div>
            <div style="margin-top: 20px; text-align: center;">
                <a href="{link}" style="display: inline-block; padding: 14px 28px; background: #c62828; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Atidaryti draft</a>
            </div>
        </div>
    </div>
    """
    send_email_notification(subject, body)


def notify_unknown_question_email(lead_email: str, client_id: str, question: str, interaction_id: int):
    """Notify about question that FAQ can't answer. Ask human for answer."""
    subject = f"❓ Gleadsy: nežinomas klausimas - {lead_email}"
    answer_url = f"{config.DASHBOARD_BASE_URL}/answer/{interaction_id}"
    body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #1565c0;">❓ Lead'as uždavė klausimą, kurio neturiu FAQ</h2>
        <table style="width: 100%; border-collapse: collapse;">
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Lead:</td>
                <td style="padding: 8px;">{lead_email}</td></tr>
            <tr><td style="padding: 8px; font-weight: bold; color: #555;">Klientas:</td>
                <td style="padding: 8px;">{client_id}</td></tr>
        </table>
        <h3 style="color: #333; margin-top: 20px;">Klausimas:</h3>
        <div style="background: #f5f5f5; padding: 15px; border-radius: 8px; border-left: 4px solid #1565c0; white-space: pre-wrap;">{question}</div>
        <div style="margin-top: 25px;">
            <p><strong>Ką daryti:</strong></p>
            <p>Atidaryk nuorodą ir parašyk atsakymą. Jis bus pridėtas į FAQ, kad kitą kartą AI jau žinotų.</p>
            <a href="{answer_url}" style="display: inline-block; padding: 12px 24px; background: #1565c0; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Atsakyti į klausimą</a>
        </div>
    </div>
    """
    send_email_notification(subject, body)
