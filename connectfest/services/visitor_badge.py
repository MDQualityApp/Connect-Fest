import os
import re
import io
import base64
import urllib.parse
import urllib.request
import ssl
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


def _load_badge_image(img_input):
	"""
	Loads a PIL Image from:
	- Local Frappe public/files path (e.g. /files/xyz.png)
	- Full URL (e.g. http://localhost:8080/files/xyz.png or https://api.connectfesttn.in/files/xyz.png)
	- Base64 data URI (data:image/...;base64,...)
	- Direct filesystem path
	Returns a PIL Image in RGBA mode, or None if invalid.
	"""
	from PIL import Image
	if not img_input or not isinstance(img_input, str):
		return None
	val = img_input.strip()
	if not val:
		return None

	# Case 1: Base64 data URI
	if val.startswith("data:image/") and ";base64," in val:
		try:
			b64_data = val.split(";base64,")[1]
			return Image.open(io.BytesIO(base64.b64decode(b64_data))).convert("RGBA")
		except Exception:
			pass

	# Case 2: Extract /files/ path from URL or relative path
	parsed = urllib.parse.urlparse(val)
	path = parsed.path if parsed.scheme else val

	if "/files/" in path:
		fname = path.split("/files/")[-1].lstrip("/")
		# Avoid loading already generated visitor badges as input logo!
		if fname.startswith("visitor_badges/") or fname.startswith("ConnectFest_Visitor_"):
			return None
		sites_path = getattr(frappe.local, "sites_path", None) or "sites"
		current_site = getattr(frappe.local, "site", None) or "connectfest.localhost"
		candidates = [
			frappe.get_site_path("public", "files", fname) if hasattr(frappe, "get_site_path") else "",
			os.path.join(sites_path, current_site, "public", "files", fname),
			os.path.join(sites_path, "connectfest", "public", "files", fname),
			os.path.join(sites_path, "connectfest.localhost", "public", "files", fname),
			os.path.join("sites", "connectfest", "public", "files", fname),
			os.path.join("sites", "connectfest.localhost", "public", "files", fname),
		]
		for cand in candidates:
			if cand and os.path.isfile(cand):
				try:
					return Image.open(cand).convert("RGBA")
				except Exception:
					pass

	# Case 3: Direct local file
	if os.path.isfile(val):
		try:
			return Image.open(val).convert("RGBA")
		except Exception:
			pass

	# Case 4: Remote HTTP/HTTPS URL (SSRF-safe & TLS-verified)
	if val.startswith("http://") or val.startswith("https://"):
		try:
			from connectfest.services.common import is_safe_public_http_url, MAX_UPLOAD_BYTES
			if not is_safe_public_http_url(val):
				return None
			ctx = ssl.create_default_context()
			req = urllib.request.Request(val, headers={"User-Agent": "ConnectFest/2027"})
			with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
				raw = resp.read(MAX_UPLOAD_BYTES + 1)
				if len(raw) > MAX_UPLOAD_BYTES:
					return None
				return Image.open(io.BytesIO(raw)).convert("RGBA")
		except Exception:
			pass

	return None


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
	has_pass_col = frappe.db.has_column("ConnectFest Visitor Registration", "visitor_image_pass") if hasattr(frappe, "db") and frappe.db else True
	pass_col_sql = ", visitor_image_pass" if has_pass_col else ""
	records = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, company_name, visitor_image
		       {pass_col_sql}, email, mobile_no,
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
	company_name = (data.get("company_name") or data.get("company") or "").strip()
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
				<div style="font-size: 16px; font-weight: 900; color: #0f172a; text-transform: uppercase; margin-bottom: 8px;">
					{full_name}
				</div>
				{f'''<div style="font-size: 9px; font-weight: 800; color: #64748b; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 2px;">COMPANY</div>
				<div style="font-size: 13px; font-weight: 800; color: #0f172a; text-transform: uppercase; margin-bottom: 8px;">
					{company_name}
				</div>''' if company_name else ''}
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
		{f'''<div class="lbl">COMPANY</div>
		<div class="val-name" style="font-size: 10pt; margin-bottom: 1.5mm;">{company_name}</div>''' if company_name else ''}
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

	from connectfest.services.common import verify_record_access_or_pass_code
	err = verify_record_access_or_pass_code(rec.get("email"), pass_code_used=bool(code), **kwargs)
	if err:
		return err

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

	from connectfest.services.common import verify_record_access_or_pass_code
	err = verify_record_access_or_pass_code(rec.get("email"), pass_code_used=bool(code), **kwargs)
	if err:
		return err

	from connectfest.services.passes import get_pass_qr_url
	base_url = get_base_url()
	pcode = rec.get("pass_code") or rec.get("registration_id")
	access_text = "VISITOR - BOTH DAYS" if (not rec.get("which_day") or str(rec.get("which_day")).lower() == "both days") else f"VISITOR - {str(rec.get('which_day')).upper()}"

	# If badge pass image not generated yet, generate and save it into visitor_image_pass!
	user_uploaded_img = rec.get("visitor_image") or ""
	pass_img = rec.get("visitor_image_pass") or ""
	if not pass_img:
		try:
			generated_img = generate_visitor_badge_image(rec)
			if generated_img:
				pass_img = generated_img
				rec["visitor_image_pass"] = pass_img
				if frappe.db.has_column("ConnectFest Visitor Registration", "visitor_image_pass"):
					frappe.db.set_value("ConnectFest Visitor Registration", rec.get("name"), "visitor_image_pass", pass_img)
					frappe.db.commit()
		except Exception:
			pass

	badge_info = {
		"pass_code": pcode,
		"registration_id": rec.get("registration_id") or rec.get("name"),
		"pass_type": "visitor_pass",
		"pass_title": "Visitor Gate Pass",
		"full_name": rec.get("full_name"),
		"company_name": rec.get("company_name") or "",
		"visitor_image": format_full_url(user_uploaded_img) if user_uploaded_img else "",
		"visitor_image_pass": format_full_url(pass_img) if pass_img else "",
		"visitor_pass_url": format_full_url(pass_img or user_uploaded_img),
		"image": format_full_url(pass_img or user_uploaded_img),
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


def _get_badge_font(bold=True, size=12):
	"""Loads Arial / TrueType font with robust cross-platform fallbacks."""
	bold_candidates = [
		"/home/mdquality-apps/.local/share/fonts/custom/Arial-Bold.ttf",
		"/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
		"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
	]
	reg_candidates = [
		"/home/mdquality-apps/.local/share/fonts/custom/Arial.ttf",
		"/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
		"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
	]
	from PIL import ImageFont
	candidates = bold_candidates if bold else reg_candidates
	for p in candidates:
		if os.path.exists(p):
			try:
				return ImageFont.truetype(p, size)
			except Exception:
				pass
	try:
		return ImageFont.load_default()
	except Exception:
		return None


def generate_visitor_badge_image(data):
	"""
	Generates a customized, ultra-high-definition (HD 300 DPI, 1008x1701), official Visitor Badge pass image:
	- Base template: visitor_badge_template.jpg upscaled with Lanczos filtering
	- Yellow banner: Visitor Name (full_name) replaces 'ACCESS: BOTH DAYS' in crisp vector font
	- Left section:
	    * COMPANY label + visitor's company_name (replaces NAME VISITOR)
	    * VENUE label + Full Event Venue ('Chennai Trade Centre, Nandambakkam')
	    * Contacts: Mr. S. Mariappan (+91 9840000000) & Mr. R. Manooj (+91 9840000000)
	- Right section:
	    * pass_code
	    * High-contrast scannable Gate Pass QR code (pyqrcode) matching entrance scanner
	    * SCAN AT ENTRY label
	- Clean rounded corner outline
	Saves PNG image to /public/files/visitor_badges/ and returns relative URL.
	"""
	from PIL import Image, ImageDraw
	import pyqrcode

	template_path = os.path.join(_TEMPLATES_DIR, "visitor_badge_template.jpg")
	if not os.path.exists(template_path):
		return ""

	# Handle data as dict or DocType object
	get_val = getattr(data, "get", None) or (lambda k, d=None: getattr(data, k, d))
	full_name = (get_val("full_name") or "VALUED VISITOR").strip()
	company_name = (get_val("company_name") or get_val("company") or "").strip()
	pass_code = (get_val("pass_code") or get_val("registration_id") or "CF27-VISITOR").strip()
	qr_payload = str(get_val("qr_payload") or pass_code).strip()

	# Ultra-High Definition Scale Factor (4x = 1344 x 2268 px @ 300+ DPI)
	SCALE = 4
	base_im = Image.open(template_path).convert("RGB")
	w, h = base_im.width * SCALE, base_im.height * SCALE
	im = base_im.resize((w, h), Image.Resampling.LANCZOS)
	draw = ImageDraw.Draw(im)

	# 0. Overlay visitor's uploaded image (photo / logo) over the goat illustration
	user_img_source = (
		get_val("visitor_image")
		or get_val("image")
		or get_val("photo")
		or get_val("avatar")
		or get_val("user_image")
		or get_val("company_logo")
		or get_val("logo")
		or ""
	)
	if user_img_source and ("visitor_badges" in str(user_img_source) or "ConnectFest_Visitor_" in str(user_img_source)):
		user_img_source = ""

	reg_id_val = get_val("registration_id") or get_val("name") or get_val("pass_code")
	if not user_img_source and reg_id_val and hasattr(frappe, "db") and frappe.db:
		try:
			cand_db = (
				frappe.db.get_value("ConnectFest Visitor Registration", {"name": reg_id_val}, "visitor_image")
				or frappe.db.get_value("ConnectFest Visitor Registration", {"registration_id": reg_id_val}, "visitor_image")
				or frappe.db.get_value("ConnectFest Visitor Registration", {"pass_code": reg_id_val}, "visitor_image")
				or ""
			)
			if cand_db and "visitor_badges" not in str(cand_db) and "ConnectFest_Visitor_" not in str(cand_db):
				user_img_source = cand_db
		except Exception:
			pass

	user_logo_im = _load_badge_image(user_img_source) if user_img_source else None
	if user_logo_im:
		# 0. Clean the white horizontal gap above the red card (y=127 to 149)
		# Completely erases any mascot horns from protruding into the white gap
		draw.rectangle([(int(22 * SCALE), int(127 * SCALE)), (int(314 * SCALE), int(149 * SCALE))], fill=(255, 255, 255))

		# 1. Cleanly re-fill the red inner card container with the exact template card red (188, 14, 38)
		# Matches template card geometry: x=20..316, y=150..306
		card_x1, card_y1 = int(20 * SCALE), int(150 * SCALE)
		card_x2, card_y2 = int(316 * SCALE), int(306 * SCALE)
		card_radius = int(14 * SCALE)
		draw.rounded_rectangle([card_x1, card_y1, card_x2, card_y2], radius=card_radius, fill=(188, 14, 38))

		# 2. Inside the red card container, place a pristine white showcase card with generous padding
		# No outer border outline so there is ZERO overlap over any template border
		pad_x = int(16 * SCALE)
		pad_y = int(8 * SCALE)
		box_x1, box_y1 = card_x1 + pad_x, card_y1 + pad_y
		box_x2, box_y2 = card_x2 - pad_x, card_y2 - pad_y
		inner_radius = int(10 * SCALE)
		draw.rounded_rectangle([box_x1, box_y1, box_x2, box_y2], radius=inner_radius, fill=(255, 255, 255))

		# 3. High-definition placement of visitor photo / logo inside the showcase card
		max_w = (box_x2 - box_x1) - int(20 * SCALE)
		max_h = (box_y2 - box_y1) - int(14 * SCALE)
		lw, lh = user_logo_im.size
		ratio = min(max_w / lw, max_h / lh)
		new_w, new_h = max(1, int(lw * ratio)), max(1, int(lh * ratio))
		logo_resized = user_logo_im.resize((new_w, new_h), Image.Resampling.LANCZOS)

		pos_x = box_x1 + ((box_x2 - box_x1) - new_w) // 2
		pos_y = box_y1 + ((box_y2 - box_y1) - new_h) // 2

		mask = logo_resized.split()[3] if logo_resized.mode == "RGBA" else None
		im.paste(logo_resized, (pos_x, pos_y), mask=mask)

	# 1. Yellow banner replacement with smooth antialiased geometry
	yellow_color = (250, 204, 21)
	poly = [
		(int(42 * SCALE), int(375 * SCALE)),
		(int(293 * SCALE), int(375 * SCALE)),
		(int(285 * SCALE), int(414 * SCALE)),
		(int(34 * SCALE), int(414 * SCALE)),
	]
	poly_mask = Image.new("L", (w, h), 0)
	poly_draw = ImageDraw.Draw(poly_mask)
	poly_draw.polygon(poly, fill=255)
	yellow_layer = Image.new("RGB", (w, h), yellow_color)
	im.paste(yellow_layer, (0, 0), mask=poly_mask)

	# Dynamic auto-scaled Visitor Name inside banner (cleaning any repeated duplicate phrases)
	cleaned_full_name = re.sub(r"(\([^\)]+\))\s*\1+", r"\1", full_name).strip()
	name_font_size = 18
	font_name = _get_badge_font(bold=True, size=int(name_font_size * SCALE))
	display_name = cleaned_full_name.upper()
	while name_font_size > 9:
		bbox = font_name.getbbox(display_name)
		if (bbox[2] - bbox[0]) <= int(225 * SCALE):
			break
		name_font_size -= 0.5
		font_name = _get_badge_font(bold=True, size=int(name_font_size * SCALE))

	draw.text((int(164 * SCALE), int(394 * SCALE)), display_name, fill=(0, 0, 0), font=font_name, anchor="mm")

	# 2. Spotless clean white canvas below banner
	draw.rectangle([(int(8 * SCALE), int(415 * SCALE)), (int(328 * SCALE), int(558 * SCALE))], fill=(255, 255, 255))

	# 3. Dynamic Event Venue from ConnectFest Settings
	event_venue = "Chennai Trade Centre, Nandambakkam"
	try:
		settings = frappe.get_single("ConnectFest Settings")
		if settings:
			event_venue = (settings.get("event_venue") or settings.get("venue") or event_venue).strip()
	except Exception:
		pass

	# Dynamic multi-line venue formatting
	if "\n" in event_venue:
		venue_lines = [l.strip() for l in event_venue.split("\n") if l.strip()]
	elif "," in event_venue:
		parts = [p.strip() for p in event_venue.split(",") if p.strip()]
		if len(parts) >= 2:
			venue_lines = [parts[0] + ",", ", ".join(parts[1:])]
		else:
			venue_lines = parts
	else:
		if len(event_venue) > 20:
			words = event_venue.split()
			mid = len(words) // 2
			venue_lines = [" ".join(words[:mid]), " ".join(words[mid:])]
		else:
			venue_lines = [event_venue]

	font_label = _get_badge_font(bold=True, size=int(7 * SCALE))

	# 4. Left side: COMPANY & VENUE
	# Company Name (auto-scaled)
	display_company = company_name if company_name else "-"
	comp_size = 12
	font_company = _get_badge_font(bold=True, size=int(comp_size * SCALE))
	while comp_size > 7:
		c_bbox = font_company.getbbox(display_company)
		if (c_bbox[2] - c_bbox[0]) <= int(175 * SCALE):
			break
		comp_size -= 0.5
		font_company = _get_badge_font(bold=True, size=int(comp_size * SCALE))

	draw.text((int(18 * SCALE), int(420 * SCALE)), "COMPANY", fill=(100, 116, 139), font=font_label)
	draw.text((int(18 * SCALE), int(431 * SCALE)), display_company, fill=(15, 23, 42), font=font_company)

	# Dynamic Venue (auto-scaled)
	venue_font_size = 8.5
	font_venue = _get_badge_font(bold=True, size=int(venue_font_size * SCALE))
	for vl in venue_lines:
		while venue_font_size > 6:
			bbox = font_venue.getbbox(vl)
			if (bbox[2] - bbox[0]) <= int(175 * SCALE):
				break
			venue_font_size -= 0.5
			font_venue = _get_badge_font(bold=True, size=int(venue_font_size * SCALE))

	draw.text((int(18 * SCALE), int(455 * SCALE)), "VENUE", fill=(100, 116, 139), font=font_label)
	cur_vy = int(466 * SCALE)
	for line in venue_lines[:2]:
		draw.text((int(18 * SCALE), cur_vy), line, fill=(51, 65, 85), font=font_venue)
		cur_vy += int(11 * SCALE)

	# 5. Right side: Gate Pass QR Code (pyqrcode with crisp exact integer modules)
	qr = pyqrcode.create(qr_payload)
	matrix = qr.code
	qr_border = 2
	matrix_size = len(matrix)
	target_qr_size = int(68 * SCALE)
	module_size = max(1, target_qr_size // (matrix_size + 2 * qr_border))
	actual_qr_size = module_size * (matrix_size + 2 * qr_border)
	qr_img = Image.new("RGB", (actual_qr_size, actual_qr_size), "white")
	qr_draw = ImageDraw.Draw(qr_img)
	for r_idx, row in enumerate(matrix):
		for c_idx, val in enumerate(row):
			if val == 1:
				x0 = (c_idx + qr_border) * module_size
				y0 = (r_idx + qr_border) * module_size
				qr_draw.rectangle([x0, y0, x0 + module_size - 1, y0 + module_size - 1], fill="black")

	qr_center_x = int(260 * SCALE)
	qr_x = qr_center_x - actual_qr_size // 2
	qr_y = int(433 * SCALE)

	# QR border box
	draw.rounded_rectangle(
		[qr_x - int(3 * SCALE), qr_y - int(3 * SCALE), qr_x + actual_qr_size + int(3 * SCALE), qr_y + actual_qr_size + int(3 * SCALE)],
		radius=int(5 * SCALE),
		outline=(203, 213, 225),
		width=max(1, int(1.5 * SCALE))
	)
	im.paste(qr_img, (qr_x, qr_y))

	# Pass code text above QR
	font_code = _get_badge_font(bold=True, size=int(8.5 * SCALE))
	draw.text((qr_center_x, int(423 * SCALE)), pass_code, fill=(30, 41, 59), font=font_code, anchor="mm")

	# SCAN AT ENTRY below QR
	font_scan = _get_badge_font(bold=True, size=int(7 * SCALE))
	draw.text((qr_center_x, int(507 * SCALE)), "SCAN AT ENTRY", fill=(67, 56, 202), font=font_scan, anchor="mm")

	# 6. Horizontal Divider Line above contacts
	draw.line([(int(18 * SCALE), int(515 * SCALE)), (int(318 * SCALE), int(515 * SCALE))], fill=(226, 232, 240), width=max(1, int(1.5 * SCALE)))

	# 7. Bottom Contacts: Side-by-Side (Left: Mr. S. Mariappan, Right: Mr. R. Manooj)
	font_contact_name = _get_badge_font(bold=True, size=int(8.5 * SCALE))
	font_contact_phone = _get_badge_font(bold=True, size=int(8.5 * SCALE))

	# Left Contact: Mr. S. Mariappan
	draw.text((int(18 * SCALE), int(521 * SCALE)), "Mr. S. Mariappan", fill=(15, 23, 42), font=font_contact_name)
	draw.text((int(18 * SCALE), int(533 * SCALE)), "+91 8825668054", fill=(220, 38, 38), font=font_contact_phone)

	# Right Contact: Mr. R. Manooj
	draw.text((int(190 * SCALE), int(521 * SCALE)), "Mr. R. Manooj", fill=(15, 23, 42), font=font_contact_name)
	draw.text((int(190 * SCALE), int(533 * SCALE)), "+91 8939291011", fill=(220, 38, 38), font=font_contact_phone)

	# 8. Crisp Rounded Card Outline with 100% Transparent Corners (RGBA - No White Corners)
	mask = Image.new("L", (w, h), 0)
	mask_draw = ImageDraw.Draw(mask)
	mask_draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(26 * SCALE), fill=255)

	final_im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
	final_im.paste(im, (0, 0), mask=mask)

	final_draw = ImageDraw.Draw(final_im)
	final_draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(26 * SCALE), outline=(0, 0, 0, 255), width=int(2 * SCALE))

	# 9. Save image to public files
	safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", pass_code)
	filename = f"ConnectFest_Visitor_{safe_code}.png"

	# Resolve site public files dir
	badges_dir = frappe.get_site_path("public", "files", "visitor_badges")
	os.makedirs(badges_dir, exist_ok=True)
	filepath = os.path.join(badges_dir, filename)

	final_im.save(filepath, format="PNG", optimize=True)
	return f"/files/visitor_badges/{filename}"

