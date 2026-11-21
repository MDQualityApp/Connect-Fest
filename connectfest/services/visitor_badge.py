import os
import re
import io
import base64
import urllib.parse
import urllib.request
import frappe
from frappe.utils import now_datetime, cint, flt
from connectfest.services.common import extract_request_data, handle_api_errors, format_full_url

try:
	import weasyprint
except (ImportError, OSError):
	weasyprint = None

# Preload template images as data URIs for fast, reliable PDF compilation
_TEMPLATES_DIR = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
	"public",
	"templates"
)


def _load_image_b64(filename):
	path = os.path.join(_TEMPLATES_DIR, filename)
	if os.path.exists(path):
		with open(path, "rb") as f:
			ext = "png" if filename.lower().endswith(".png") else "jpeg"
			return f"data:image/{ext};base64,{base64.b64encode(f.read()).decode('utf-8')}"
	return ""


CONNECTFEST_LOGO_B64 = _load_image_b64("connectfest_logo.png")
BNI_LOGO_B64 = _load_image_b64("bni_logo.png")


def get_visitor_badge_data(pass_code=None, registration_id=None, mobile_no=None, email=None):
	"""Fetches visitor record for badge generation."""
	conds = []
	vals = {}

	clean_code = (pass_code or "").strip()
	clean_reg = (registration_id or "").strip()
	clean_mob = (mobile_no or "").strip()
	clean_em = (email or "").strip().lower()

	if clean_code:
		conds.append("pass_code = %(code)s OR name = %(code)s")
		vals["code"] = clean_code
	if clean_reg:
		conds.append("registration_id = %(reg)s OR name = %(reg)s")
		vals["reg"] = clean_reg
	if clean_mob:
		conds.append("mobile_no = %(mob)s")
		vals["mob"] = clean_mob
	if clean_em:
		conds.append("email = %(em)s")
		vals["em"] = clean_em

	if not conds:
		return None

	where_clause = " OR ".join(conds)
	records = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, visitor_image, email, mobile_no,
		       which_day, pass_code, qr_payload, status, checked_in, checked_in_at,
		       area_of_interest, creation
		FROM `tabConnectFest Visitor Registration`
		WHERE {where_clause}
		ORDER BY creation DESC
		LIMIT 1
	""", vals, as_dict=True)

	if records:
		return records[0]
	return None


def generate_visitor_badge_html(data, is_email=False):
	"""
	Generates clean, high-contrast Visitor Badge HTML:
	- Top dark red header with lanyard punch hole
	- ConnectFest & BNI logos with Grand Expo dates
	- Bold condensed 'VISITOR' headline & access ribbon
	- High-contrast QR Code with 'SCAN AT ENTRY' (No photo box)
	- Bottom details with Name, Venue, and Dates
	"""
	from connectfest.services.notifications import get_base_url
	from connectfest.services.passes import generate_local_qr_png_bytes, get_pass_qr_url

	full_name = (data.get("full_name") or "Valued Visitor").strip()
	pass_code = (data.get("pass_code") or data.get("registration_id") or "CF27-VISITOR").strip()
	which_day = (data.get("which_day") or "Both days").strip()

	# Dynamic Event details from Settings
	event_dates = "20 & 21 Feb 2027"
	event_venue = "Chennai Trade Centre, Nandambakkam"
	try:
		settings = frappe.get_single("ConnectFest Settings")
		event_dates = settings.get("event_dates") or event_dates
		event_venue = settings.get("event_venue") or event_venue
	except Exception:
		pass

	# Access ribbon text
	access_text = "ACCESS: BOTH DAYS"
	if which_day.lower() != "both days":
		access_text = f"ACCESS: {which_day.upper()}"

	base_url = get_base_url()
	qr_url = get_pass_qr_url(pass_code)
	download_qr_url = f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(pass_code))}"

	# Pre-generate local base64 QR for 100% offline PDF compilation
	qr_png = generate_local_qr_png_bytes(pass_code, scale=10, border=2)
	qr_b64 = f"data:image/png;base64,{base64.b64encode(qr_png).decode('utf-8')}"

	# If generating for email body (uses table-based responsive email styling)
	if is_email:
		return f"""
		<div style="max-width: 320px; margin: 0 auto; background: #ffffff; border: 2px solid #0f172a; border-radius: 18px; overflow: hidden; box-shadow: 0 8px 20px rgba(0,0,0,0.1); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
			<!-- Header -->
			<div style="background-color: #b91c1c; background-image: linear-gradient(180deg, #b91c1c 0%, #991b1b 100%); padding: 10px 16px 12px 16px; text-align: center; color: #ffffff;">
				<!-- Lanyard Slot -->
				<div style="width: 44px; height: 8px; background: #000000; border-radius: 4px; margin: 0 auto 8px auto;"></div>
				<table style="width: 100%; border-collapse: collapse;">
					<tr>
						<td style="width: 48%; text-align: right; vertical-align: middle; padding: 0 6px 0 0;">
							<img src="{CONNECTFEST_LOGO_B64}" alt="ConnectFest" style="height: 28px; width: auto; max-width: 100px; display: inline-block; vertical-align: middle;" />
						</td>
						<td style="width: 4%; text-align: center; vertical-align: middle; padding: 0;">
							<div style="border-left: 1.5px solid rgba(255,255,255,0.7); height: 24px; width: 1px; margin: 0 auto;"></div>
						</td>
						<td style="width: 48%; text-align: left; vertical-align: middle; padding: 0 0 0 6px;">
							<img src="{BNI_LOGO_B64}" alt="BNI" style="height: 22px; width: auto; max-width: 75px; display: inline-block; vertical-align: middle;" /><br>
							<span style="font-size: 8px; font-weight: 800; color: #ffffff; letter-spacing: 0.5px; display: block; margin-top: 1px;">CHENNAI NORTH</span>
						</td>
					</tr>
				</table>
				<div style="font-size: 8.5px; font-weight: 800; letter-spacing: 1.5px; text-transform: uppercase; color: #ffffff; margin-top: 7px;">
					GRAND EXPO &bull; {event_dates.upper()}
				</div>
			</div>

			<!-- Title -->
			<div style="text-align: center; margin: 16px 0 4px 0;">
				<div style="font-family: Impact, 'Arial Black', Arial, sans-serif; font-size: 34px; font-weight: 900; font-style: italic; letter-spacing: 2px; color: #000000; line-height: 1; text-transform: uppercase;">
					VISITOR
				</div>
			</div>

			<!-- Access Banner -->
			<div style="text-align: center; margin: 4px 0 14px 0;">
				<div style="display: inline-block; background: #f59e0b; color: #000000; font-family: Impact, 'Arial Black', Arial, sans-serif; font-size: 15px; font-weight: 900; letter-spacing: 0.5px; padding: 4px 16px; transform: skewX(-8deg); border-radius: 2px; text-transform: uppercase;">
					{access_text}
				</div>
			</div>

			<!-- Center QR Code Box -->
			<div style="text-align: center; padding: 0 16px 14px 16px;">
				<div style="font-family: monospace; font-size: 12px; font-weight: 800; color: #1e293b; margin-bottom: 6px; letter-spacing: 1px;">
					{pass_code}
				</div>
				<a href="{download_qr_url}" target="_blank" download="ConnectFest_Pass_QR_{pass_code}.png" title="Click to download QR code" style="display: inline-block; text-decoration: none;">
					<img src="{qr_url}" alt="Gate Pass QR" style="width: 140px; height: 140px; border: 1.5px solid #cbd5e1; border-radius: 10px; padding: 6px; background: #ffffff; display: block; margin: 0 auto;" />
					<span style="font-size: 11px; color: #b91c1c; font-weight: 700; display: block; margin-top: 6px; text-decoration: underline;">📥 Click to Download QR</span>
				</a>
				<div style="font-size: 9px; font-weight: 800; color: #4338ca; letter-spacing: 1px; margin-top: 6px; text-transform: uppercase;">
					SCAN AT ENTRY
				</div>
			</div>

			<!-- Bottom Details -->
			<div style="padding: 0 16px 18px 16px; border-top: 1px solid #f1f5f9; padding-top: 12px;">
				<div style="font-size: 9px; font-weight: 800; color: #64748b; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 2px;">NAME</div>
				<div style="font-size: 16px; font-weight: 900; color: #0f172a; text-transform: uppercase; margin-bottom: 10px;">
					{full_name}
				</div>
				<div style="font-size: 9px; font-weight: 800; color: #64748b; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 2px;">VENUE</div>
				<div style="font-size: 11px; font-weight: 600; color: #334155; line-height: 1.3;">
					{event_venue}
				</div>
			</div>
		</div>
		"""

	# Default PDF / Print HTML Layout (Zero Photo Box, Pure QR Focus)
	return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Visitor Pass - {full_name} ({pass_code})</title>
<style>
	@page {{
		size: 90mm 152mm;
		margin: 0;
	}}
	* {{
		box-sizing: border-box;
	}}
	body {{
		margin: 0;
		padding: 0;
		font-family: Arial, "Helvetica Neue", Helvetica, sans-serif;
		background: #ffffff;
		-webkit-print-color-adjust: exact;
	}}
	.badge-card {{
		width: 90mm;
		height: 152mm;
		margin: 0 auto;
		background: #ffffff;
		border: 2px solid #000000;
		border-radius: 12mm;
		overflow: hidden;
		position: relative;
		box-sizing: border-box;
		text-align: center;
	}}
	.lanyard-slot {{
		width: 14mm;
		height: 3mm;
		background: #000000;
		border-radius: 2mm;
		margin: 3mm auto 2mm auto;
	}}
	.badge-header {{
		background-color: #b91c1c;
		background: linear-gradient(180deg, #b91c1c 0%, #991b1b 100%);
		padding: 1.5mm 4mm 3mm 4mm;
		text-align: center;
		color: #ffffff;
	}}
	.header-table {{
		width: 100%;
		border-collapse: collapse;
	}}
	.header-table td {{
		vertical-align: middle;
	}}
	.header-sub {{
		font-size: 7pt;
		font-weight: 800;
		letter-spacing: 1.5px;
		text-transform: uppercase;
		color: #ffffff;
		margin-top: 1.5mm;
	}}
	.vis-title {{
		font-family: Impact, "Arial Black", Arial, sans-serif;
		font-size: 32pt;
		font-style: italic;
		font-weight: 900;
		letter-spacing: 2px;
		color: #0a0a0a;
		text-align: center;
		margin-top: 4mm;
		margin-bottom: 0;
		line-height: 1;
	}}
	.ribbon-wrap {{
		text-align: center;
		margin: 2mm 0 4mm 0;
	}}
	.access-ribbon {{
		background: #f59e0b;
		color: #000000;
		font-family: Impact, "Arial Black", Arial, sans-serif;
		font-size: 12pt;
		font-weight: 900;
		letter-spacing: 0.5px;
		padding: 1.5mm 6mm;
		display: inline-block;
		transform: skewX(-8deg);
	}}
	.pcode-txt {{
		font-family: monospace;
		font-size: 9.5pt;
		font-weight: 800;
		color: #1f2937;
		text-align: center;
		margin-bottom: 1.5mm;
		letter-spacing: 1px;
	}}
	.qr-box {{
		display: inline-block;
		padding: 1.5mm;
		border: 0.8mm solid #cbd5e1;
		border-radius: 3mm;
		background: #ffffff;
	}}
	.qr-img {{
		width: 36mm;
		height: 36mm;
		display: block;
		margin: 0 auto;
	}}
	.scan-lbl {{
		font-size: 6.5pt;
		font-weight: 800;
		color: #4338ca;
		letter-spacing: 1px;
		text-align: center;
		margin-top: 1.5mm;
		text-transform: uppercase;
	}}
	.info-box {{
		padding: 0 6mm;
		margin-top: 3.5mm;
		text-align: left;
		border-top: 0.4mm solid #f1f5f9;
		padding-top: 2.5mm;
	}}
	.lbl {{
		font-size: 6.5pt;
		font-weight: 800;
		color: #6b7280;
		letter-spacing: 0.8px;
		text-transform: uppercase;
		margin-bottom: 0.5mm;
	}}
	.val-name {{
		font-size: 12pt;
		font-weight: 900;
		color: #111827;
		text-transform: uppercase;
		margin-bottom: 2mm;
		line-height: 1.2;
	}}
	.val-venue {{
		font-size: 8pt;
		font-weight: 700;
		color: #374151;
		line-height: 1.25;
	}}
</style>
</head>
<body>
<div class="badge-card">
	<div class="badge-header">
		<div class="lanyard-slot"></div>
		<table class="header-table">
			<tr>
				<td style="width: 48%; text-align: right; vertical-align: middle; padding-right: 2mm;">
					<img src="{CONNECTFEST_LOGO_B64}" style="height: 8.5mm; width: auto; max-width: 28mm; vertical-align: middle;" alt="ConnectFest">
				</td>
				<td style="width: 4%; text-align: center; vertical-align: middle;">
					<div style="border-left: 0.8px solid #ffffff; height: 7mm; width: 1px; margin: 0 auto;"></div>
				</td>
				<td style="width: 48%; text-align: left; vertical-align: middle; padding-left: 2mm;">
					<img src="{BNI_LOGO_B64}" style="height: 7mm; width: auto; max-width: 22mm; vertical-align: middle;" alt="BNI">
					<div style="font-size: 4.8pt; font-weight: bold; color: #ffffff; line-height: 1; margin-top: 0.5mm;">CHENNAI NORTH</div>
				</td>
			</tr>
		</table>
		<div class="header-sub">GRAND EXPO &bull; {event_dates.upper()}</div>
	</div>

	<div class="vis-title">VISITOR</div>
	<div class="ribbon-wrap">
		<div class="access-ribbon">{access_text}</div>
	</div>

	<div style="margin-top: 1mm;">
		<div class="pcode-txt">{pass_code}</div>
		<div class="qr-box">
			<img src="{qr_b64}" class="qr-img" alt="Pass QR">
		</div>
		<div class="scan-lbl">SCAN AT ENTRY</div>
	</div>

	<div class="info-box">
		<div class="lbl">NAME</div>
		<div class="val-name">{full_name}</div>
		<div class="lbl">VENUE</div>
		<div class="val-venue">{event_venue}</div>
	</div>
</div>
</body>
</html>"""


def generate_visitor_badge_pdf_bytes(data):
	"""Compiles high-resolution PDF bytes for the Visitor Badge using WeasyPrint with graceful fallback."""
	html_content = generate_visitor_badge_html(data, is_email=False)
	if weasyprint:
		try:
			return weasyprint.HTML(string=html_content).write_pdf()
		except Exception as e:
			try:
				frappe.logger("connectfest").warning(f"[WeasyPrint Visitor Badge Warning] {str(e)}")
			except Exception:
				pass

	try:
		from frappe.utils.pdf import get_pdf
		return get_pdf(html_content)
	except Exception as e:
		frappe.throw(f"Visitor Badge PDF compilation failed: {str(e)}")


@handle_api_errors("ConnectFest Download Visitor Badge Error")
def download_visitor_badge(pass_code=None, registration_id=None, mobile_no=None, email=None, **kwargs):
	"""
	Downloads the official vertical Visitor Gate Pass Badge Card as a printable PDF.
	Accessible publicly by pass_code, registration_id, mobile_no, or email.
	"""
	data = extract_request_data(kwargs)
	code = (pass_code or data.get("pass_code") or data.get("code") or "").strip()
	reg_id = (registration_id or data.get("registration_id") or data.get("id") or "").strip()
	mob = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()
	em = (email or data.get("email") or "").strip().lower()

	rec = get_visitor_badge_data(pass_code=code, registration_id=reg_id, mobile_no=mob, email=em)
	if not rec:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": "No confirmed visitor registration record found."}

	pdf_bytes = generate_visitor_badge_pdf_bytes(rec)
	safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", str(rec.get("pass_code") or rec.get("registration_id") or "VISITOR"))
	filename = f"ConnectFest_Visitor_Pass_{safe_code}.pdf"

	frappe.local.response.filename = filename
	frappe.local.response.filecontent = pdf_bytes
	frappe.local.response.type = "download"
	frappe.local.response.content_type = "application/pdf"


@handle_api_errors("ConnectFest Get Visitor Badge Error")
def get_visitor_badge(pass_code=None, registration_id=None, mobile_no=None, email=None, **kwargs):
	"""
	API returning full visitor badge details with direct URLs for QR and full badge card PDF.
	Accessible publicly by pass_code, registration_id, mobile_no, or email.
	"""
	from connectfest.services.notifications import get_base_url
	data = extract_request_data(kwargs)
	code = (pass_code or data.get("pass_code") or data.get("code") or "").strip()
	reg_id = (registration_id or data.get("registration_id") or data.get("id") or "").strip()
	mob = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()
	em = (email or data.get("email") or "").strip().lower()

	rec = get_visitor_badge_data(pass_code=code, registration_id=reg_id, mobile_no=mob, email=em)
	if not rec:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": "No confirmed visitor registration record found."}

	from connectfest.services.passes import get_pass_qr_url
	base_url = get_base_url()
	pcode = rec.get("pass_code") or rec.get("registration_id")
	access_text = "VISITOR - BOTH DAYS" if (not rec.get("which_day") or str(rec.get("which_day")).lower() == "both days") else f"VISITOR - {str(rec.get('which_day')).upper()}"

	badge_info = {
		"pass_code": pcode,
		"registration_id": rec.get("registration_id") or rec.get("name"),
		"pass_type": "visitor_pass",
		"pass_title": "Visitor Gate Pass",
		"full_name": rec.get("full_name"),
		"image": format_full_url(rec.get("visitor_image")),
		"visitor_image": format_full_url(rec.get("visitor_image")),
		"which_day": rec.get("which_day") or "Both days",
		"access": access_text,
		"email": rec.get("email"),
		"mobile_no": rec.get("mobile_no"),
		"area_of_interest": rec.get("area_of_interest") or "",
		"status": rec.get("status") or "Registered",
		"checked_in": cint(rec.get("checked_in") or 0),
		"checked_in_at": str(rec.get("checked_in_at") or "") if rec.get("checked_in") else None,
		"venue": "Chennai Trade Centre, Nandambakkam",
		"dates": "20 & 21 Feb 2027",
		"qr_payload": rec.get("qr_payload") or pcode,
		"qr_url": get_pass_qr_url(pcode),
		"qr_download_url": f"{base_url}/api/method/connectfest.api.download_pass_qr?code={urllib.parse.quote(str(pcode))}",
		"badge_download_url": f"{base_url}/api/method/connectfest.api.download_visitor_badge?code={urllib.parse.quote(str(pcode))}",
	}

	return {
		"status": "success",
		"badge": badge_info,
		"pass": badge_info,
		"visitor_pass": badge_info,
	}

