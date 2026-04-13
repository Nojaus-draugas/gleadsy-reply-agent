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
    subject = f"⚠️ Gleadsy: reikia peržiūros — {lead_email}"
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
    subject = f"🔥 Gleadsy: INTERESTED lead — {lead_email}"
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


def notify_unknown_question_email(lead_email: str, client_id: str, question: str, interaction_id: int):
    """Notify about question that FAQ can't answer. Ask human for answer."""
    subject = f"❓ Gleadsy: nežinomas klausimas — {lead_email}"
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
