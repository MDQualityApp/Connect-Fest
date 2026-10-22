import os
import base64
import frappe
from frappe.utils import now_datetime, formatdate, flt
import weasyprint

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
			return f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode('utf-8')}"
	return ""

TEXPO_LOGO_B64 = _load_image_b64("texpo_logo.jpg")
TIEMA_LOGO_B64 = _load_image_b64("tiema_logo.jpg")
SIGNATURE_B64 = _load_image_b64("signature.jpg")


def format_doc_date(dt_val):
	if not dt_val:
		return now_datetime().strftime("%d.%m.%Y")
	if isinstance(dt_val, str):
		try:
			from dateutil.parser import parse
			dt = parse(dt_val)
			return dt.strftime("%d.%m.%Y")
		except Exception:
			return str(dt_val)[:10]
	try:
		return dt_val.strftime("%d.%m.%Y")
	except Exception:
		return now_datetime().strftime("%d.%m.%Y")


def generate_stall_allocation_html(data):
	"""
	Constructs high-fidelity 3-page HTML for Stall Allocation Letter / Sales Invoice
	matching the official TEXPO 2026 / TIEMA template format.
	"""
	company_name = data.get("company_name") or data.get("full_name") or "Valued Exhibitor"
	booth_no = data.get("stall_numbers") or data.get("stall_number") or "Assigned"
	hall_no = data.get("hall_number") or data.get("hall_no") or "2"
	area_allotted = data.get("dimensions") or data.get("area_allotted") or "2 X 2"
	category = (data.get("stall_category") or data.get("tier") or "EXHIBITOR").upper()
	sponsor = data.get("sponsor") or ""
	
	# Clean booth number for Ref No.
	clean_booth = str(booth_no).replace(" ", "").replace("-", "")
	ref_no = data.get("ref_no") or f"TEXPO2026/EXH/PA/{clean_booth}"
	doc_date = format_doc_date(data.get("payment_date") or data.get("creation"))

	html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Stall Allocation - {booth_no}</title>
<style>
	@page {{
		size: A4 portrait;
		margin-top: 30mm;
		margin-bottom: 28mm;
		margin-left: 20mm;
		margin-right: 20mm;
		@top-right {{
			content: element(pageHeader);
			vertical-align: bottom;
			padding-bottom: 5mm;
		}}
		@bottom-center {{
			content: element(pageFooter);
			vertical-align: top;
			width: 100%;
		}}
	}}
	* {{
		box-sizing: border-box;
	}}
	body {{
		font-family: Arial, "Helvetica Neue", Helvetica, sans-serif;
		color: #000000;
		margin: 0;
		padding: 0;
		font-size: 13.5px;
		line-height: 1.55;
	}}

	div.page-header {{
		position: running(pageHeader);
		text-align: right;
	}}
	.texpo-logo {{
		height: 48px;
		width: auto;
	}}

	div.page-footer {{
		position: running(pageFooter);
		width: 100%;
		border-top: 1.5px solid #0891b2;
		padding-top: 6px;
	}}
	.footer-table {{
		width: 100%;
		border-collapse: collapse;
	}}
	.footer-table td {{
		vertical-align: middle;
		padding: 0;
	}}
	.tiema-logo {{
		height: 38px;
		width: auto;
	}}
	.footer-text {{
		font-size: 9.5px;
		color: #0284c7;
		text-align: right;
		line-height: 1.45;
		font-family: Arial, sans-serif;
	}}

	.page-break {{
		page-break-before: always;
	}}

	/* Page 1 Specific */
	.meta-table {{
		width: 100%;
		margin-bottom: 22px;
		font-size: 13.5px;
	}}
	.meta-table td {{
		padding: 0;
	}}
	.subject {{
		text-align: center;
		font-weight: bold;
		font-size: 14.5px;
		margin: 24px 0 22px 0;
	}}
	.content-p {{
		margin: 0 0 15px 0;
		text-align: justify;
	}}
	.allotment-table {{
		width: 440px;
		border-collapse: collapse;
		margin: 18px 0 22px 0;
		font-size: 13px;
	}}
	.allotment-table td {{
		border: 1px solid #000000;
		padding: 6px 14px;
	}}
	.signature-area {{
		margin-top: 25px;
	}}
	.sig-img {{
		height: 38px;
		margin: 4px 0;
	}}

	/* Terms & Conditions Pages */
	.tc-title {{
		text-align: center;
		font-weight: bold;
		font-size: 14.5px;
		margin-bottom: 18px;
	}}
	.tc-section-title {{
		font-weight: bold;
		font-size: 13.5px;
		margin-top: 14px;
		margin-bottom: 6px;
	}}
	ul.tc-list {{
		margin: 4px 0 12px 0;
		padding-left: 22px;
	}}
	ul.tc-list li {{
		margin-bottom: 6px;
		text-align: justify;
	}}
</style>
</head>
<body>

<!-- RUNNING HEADER & FOOTER -->
<div class="page-header">
	<img src="{TEXPO_LOGO_B64}" class="texpo-logo" alt="TEXPO">
</div>

<div class="page-footer">
	<table class="footer-table">
		<tr>
			<td style="width: 120px; text-align: left;">
				<img src="{TIEMA_LOGO_B64}" class="tiema-logo" alt="TIEMA">
			</td>
			<td class="footer-text">
				PP3, TIEMA CENTRE, SIDCO INDUSTRIAL ESTATE, THIRUMUDIVAKKAM, CHENNAI-132<br>
				admin@tiema.co.in; texpo@tiema.co.in &nbsp; Phone: +91 70929 57777 / +91 74490 12333
			</td>
		</tr>
	</table>
</div>

<!-- PAGE 1 CONTENT -->
<div>
	<table class="meta-table">
		<tr>
			<td style="width: 60%;"><strong>Ref. No.:</strong> {ref_no}</td>
			<td style="width: 40%; text-align: right;"><strong>Date:</strong> {doc_date}</td>
		</tr>
		<tr>
			<td colspan="2" style="padding-top: 10px;"><strong>To:</strong> {company_name}</td>
		</tr>
	</table>

	<div class="subject">Subject: Intimation of Stall Allocation</div>

	<p class="content-p"><strong>Dear Exhibitor,</strong></p>
	<p class="content-p">We hope this message finds you well.</p>
	<p class="content-p">We are pleased to inform you that, as part of our ongoing efforts to enhance the overall exhibition experience, certain upgrades have been implemented across the exhibition area. These improvements are designed to provide better facilities, improved layout, and an enhanced environment for both exhibitors and visitors.</p>
	<p class="content-p">Kindly note your stall details below:</p>

	<table class="allotment-table">
		<tr>
			<td style="width: 50%;"></td>
			<td style="width: 50%; font-weight: bold;">Allotment</td>
		</tr>
		<tr>
			<td>Hall Number:</td>
			<td><strong>{hall_no}</strong></td>
		</tr>
		<tr>
			<td>Booth No.:</td>
			<td><strong>{booth_no}</strong></td>
		</tr>
		<tr>
			<td>Area Allotted (Sq. M):</td>
			<td><strong>{area_allotted}</strong></td>
		</tr>
		<tr>
			<td>Participation Category:</td>
			<td><strong>{category}</strong></td>
		</tr>
		<tr>
			<td>If Sponsor</td>
			<td>{sponsor}</td>
		</tr>
	</table>

	<p class="content-p">We request you to take note and ensure that all further correspondence, logistics, and setup arrangements are aligned with the stall number.</p>
	<p class="content-p">Our team remains available to assist you with any clarification or support you may require during this transition.</p>
	<p class="content-p">We appreciate your continued cooperation and understanding as we strive to deliver an improved exhibition experience.</p>
	<p class="content-p">We look forward to your support and wish you a successful event.</p>

	<div class="signature-area">
		<div>Warm regards,</div>
		<img src="{SIGNATURE_B64}" class="sig-img" alt="Signature"><br>
		<strong>TEXPO 2026 Committee</strong>
	</div>
</div>

<!-- PAGE 2 CONTENT -->
<div class="page-break"></div>
<div>
	<div class="tc-title">TERMS AND CONDITIONS:</div>

	<div class="tc-section-title">1. Stall Usage:</div>
	<ul class="tc-list">
		<li>Exhibitors must use stalls only for approved products/services.</li>
		<li>Any activity deemed unsafe, unlawful, or disruptive will lead to immediate termination of participation.</li>
		<li>Exhibitors are responsible for maintaining cleanliness and safety within their stall.</li>
	</ul>

	<div class="tc-section-title">2. Setup &amp; Dismantling:</div>
	<ul class="tc-list">
		<li>Stall setup must be completed within the designated time slots provided by organizers.</li>
		<li>Dismantling before the official closing time is not permitted.</li>
		<li>Exhibitors are liable for any damage caused to stall structures, venue property, or equipment.</li>
	</ul>

	<div class="tc-section-title">3. Compliance &amp; Safety:</div>
	<ul class="tc-list">
		<li>Exhibitors must comply with venue regulations, fire safety norms, and government laws.</li>
		<li>Electrical connections, heavy machinery, or hazardous materials require prior approval.</li>
		<li>Insurance coverage for exhibits and personnel is the responsibility of the exhibitor.</li>
	</ul>

	<div class="tc-section-title">4. Liability:</div>
	<ul class="tc-list">
		<li>Organizers are not responsible for loss, theft, or damage to exhibitor property.</li>
		<li>Exhibitors indemnify organizers against claims arising from their participation.</li>
	</ul>

	<div class="tc-section-title">5. Promotion &amp; Branding:</div>
	<ul class="tc-list">
		<li>Organizers may use exhibitor names, logos, and stall images for event promotion.</li>
		<li>Exhibitors must adhere to branding guidelines provided by organizers.</li>
	</ul>

	<div class="tc-section-title">6. Force Majeure:</div>
	<p class="content-p" style="font-size: 12.5px; line-height: 1.5;">
		The term Force Majeure shall include, but not be limited to: earthquakes, floods, fires, explosions, acts of God, acts of state, wars, terrorism, actions of any Government, tempests, pandemics, epidemics, quarantine restrictions, serious industrial disputes, strikes, lockouts, local unrest, shipwrecks, civil war, acts of the public enemy (including block of passage and riots), directives by any statutory authority, compliance with directives or orders of any law court, directives from the State Government or Government of India, and any other events or decisions beyond the control of the Organiser.
	</p>
</div>

<!-- PAGE 3 CONTENT -->
<div class="page-break"></div>
<div>
	<div class="tc-section-title" style="margin-top: 10px;">7. Organiser's Rights:</div>
	<p class="content-p" style="margin-bottom: 6px;">In the event of Force Majeure, the Organiser reserves the right to:</p>
	<ul class="tc-list">
		<li>Alter the opening date and/or duration of the exhibition; or</li>
		<li>Cancel the exhibition in its entirety.</li>
		<li><strong>Effect of Change in Dates/Duration:</strong> If the exhibition dates or duration are altered, all rules, regulations, and agreements between the Exhibitor and the Organiser shall remain valid and binding.</li>
		<li><strong>Effect of Cancellation:</strong> In the event of cancellation of the exhibition:
			<ul style="margin: 4px 0 4px 0; padding-left: 20px;">
				<li>Stall rental fees will be refunded to the Exhibitor.</li>
				<li>Refunds will be subject to deduction of non-recoverable proportionate costs already incurred by the Organiser.</li>
			</ul>
		</li>
	</ul>

	<div class="tc-section-title" style="margin-top: 24px;">8. Jurisdiction:</div>
	<p class="content-p">All disputes arising under these conditions shall be subject to the jurisdiction of Chennai courts</p>
</div>

</body>
</html>"""
	return html


def generate_stall_allocation_pdf_bytes(data):
	"""Compiles high-resolution PDF bytes using WeasyPrint."""
	html_content = generate_stall_allocation_html(data)
	return weasyprint.HTML(string=html_content).write_pdf()


def get_registration_allocation_data(registration_id=None, mobile_no=None, email=None):
	"""
	Fetches complete registration details along with linked stall dimensions and zone
	to populate the allocation document accurately.
	"""
	if not registration_id and not mobile_no and not email:
		return None

	conds = []
	vals = {}
	if registration_id:
		conds.append("(r.registration_id = %(id)s OR r.name = %(id)s)")
		vals["id"] = registration_id
	if mobile_no:
		conds.append("r.mobile_no = %(mobile)s")
		vals["mobile"] = mobile_no
	if email:
		conds.append("r.email = %(email)s")
		vals["email"] = email

	where_clause = " OR ".join(conds)

	query = f"""
		SELECT r.name, r.registration_id, r.full_name, r.company_name, r.email, r.mobile_no,
		       r.location, r.industry, r.segment, r.is_bni_member, r.region_name, r.chapter_name,
		       r.stall_number, r.stall_category, r.amount, r.payment_status, r.payment_mode,
		       r.razorpay_payment_id, r.payment_date, r.creation,
		       s.dimensions, s.zone, s.block, s.tier
		FROM `tabConnectFest Registration` r
		LEFT JOIN `tabConnectFest Stall` s ON s.stall_number = r.stall_number
		WHERE {where_clause}
		ORDER BY r.creation DESC
		LIMIT 1
	"""
	records = frappe.db.sql(query, vals, as_dict=True)
	if not records:
		return None

	data = records[0]

	# Parse dimensions
	dim = data.get("dimensions") or "2 X 2"
	# If zone has hall, parse e.g. "Hall 2" -> "2"
	hall = "2"
	zone = str(data.get("zone") or "")
	for ch in zone:
		if ch.isdigit():
			hall = ch
			break

	data["hall_no"] = hall
	data["area_allotted"] = dim
	return data
