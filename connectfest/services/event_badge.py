import os
import re
import io
import json
import frappe
from frappe.utils import now_datetime, cint
from connectfest.services.common import extract_request_data, handle_api_errors, format_full_url, safe_str
from connectfest.services.visitor_badge import _get_badge_font


def get_event_badge_data(pass_code=None, registration_id=None, mobile_no=None, email=None):
	"""Fetches BNI Event registration record for badge generation."""
	conds = []
	vals = {}

	clean_code = (pass_code or "").strip()
	clean_reg = (registration_id or "").strip()
	clean_mob = (mobile_no or "").strip()
	clean_em = (email or "").strip().lower()

	if clean_code:
		conds.append("(pass_code = %(code)s OR name = %(code)s)")
		vals["code"] = clean_code
	if clean_reg:
		conds.append("(registration_id = %(reg)s OR name = %(reg)s)")
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
	has_pass_col = frappe.db.has_column("ConnectFest BNI Event Registration", "event_pass_url") if hasattr(frappe, "db") and frappe.db else True
	pass_col_sql = ", event_pass_url" if has_pass_col else ""
	records = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, company_name, mobile_no, email,
		       chapter_name, region_name, location, industry, segment, event_name,
		       pass_code, qr_payload, status, checked_in, checked_in_at, creation
		       {pass_col_sql}
		FROM `tabConnectFest BNI Event Registration`
		WHERE {where_clause}
		ORDER BY creation DESC
		LIMIT 1
	""", vals, as_dict=True)

	if records:
		return records[0]
	return None


def generate_event_badge_image(data):
	"""
	Generates an ultra-high-definition, official BNI / CNR Event Pass Badge image:
	- Matches official physical badge card design (Red header with lanyard clip, CNR / BNI role header,
	  crisp dynamic Attendee Name & Venue, scannable Gate Pass QR code, and Entry Gate Contacts)
	- Saves PNG to /public/files/event_passes/ConnectFest_Event_{pass_code}.png
	- Stores relative URL in tabConnectFest BNI Event Registration (event_pass_url).
	- Returns relative URL.
	"""
	from PIL import Image, ImageDraw
	import pyqrcode

	get_val = getattr(data, "get", None) or (lambda k, d=None: getattr(data, k, d))

	full_name = (get_val("full_name") or get_val("name") or "Valued Delegate").strip()
	pass_code = (get_val("pass_code") or get_val("registration_id") or get_val("name") or "CF27-EVENT").strip()
	raw_qr = get_val("qr_payload") or pass_code
	qr_payload = str(raw_qr).strip()

	chapter_name = (get_val("chapter_name") or "").strip()
	is_cnr = bool("cnr" in chapter_name.lower() or "cnr" in pass_code.lower() or get_val("is_cnr_member"))

	pill_txt = "EXPO VISITOR · CNR" if is_cnr else ("BNI MEMBER" if chapter_name else "EVENT ATTENDEE")
	role_header = "CNR MEMBER" if is_cnr else ("BNI MEMBER" if chapter_name else "EVENT ATTENDEE")
	venue_text = "Chennai Trade Centre, Nandambakkam"

	W, H = 600, 860
	im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
	draw = ImageDraw.Draw(im)

	card_x1, card_y1 = 25, 20
	card_x2, card_y2 = 575, 840
	card_w = card_x2 - card_x1
	card_h = card_y2 - card_y1
	card_r = 28

	card_im = Image.new("RGBA", (card_w, card_h), (255, 255, 255, 255))
	cdraw = ImageDraw.Draw(card_im)

	red_h = int(card_h * 0.58)
	cdraw.rectangle([0, 0, card_w, red_h], fill=(207, 32, 48, 255))
	cdraw.rectangle([0, red_h, card_w, card_h], fill=(255, 255, 255, 255))

	# Side notch cutouts matching badge card template
	notch_r = 16
	cdraw.ellipse([-notch_r, red_h - notch_r, notch_r, red_h + notch_r], fill=(255, 255, 255, 255))
	cdraw.ellipse([card_w - notch_r, red_h - notch_r, card_w + notch_r, red_h + notch_r], fill=(255, 255, 255, 255))

	# Round mask for entire card
	mask = Image.new("L", (card_w, card_h), 0)
	mdraw = ImageDraw.Draw(mask)
	mdraw.rounded_rectangle([0, 0, card_w, card_h], radius=card_r, fill=255)
	card_im.putalpha(mask)
	im.paste(card_im, (card_x1, card_y1), mask=card_im)

	# Lanyard Clip at top
	clip_w, clip_h = 70, 18
	clip_x = W // 2 - clip_w // 2
	clip_y = card_y1 - 6
	draw.rounded_rectangle([clip_x, clip_y, clip_x + clip_w, clip_y + clip_h], radius=6, fill=(203, 213, 225, 255), outline=(148, 163, 184, 255), width=1)
	slot_w, slot_h = 42, 6
	slot_x = W // 2 - slot_w // 2
	slot_y = clip_y + 6
	draw.rounded_rectangle([slot_x, slot_y, slot_x + slot_w, slot_y + slot_h], radius=3, fill=(15, 23, 42, 255))

	# Top-Right Pill: "EXPO VISITOR · CNR"
	font_pill = _get_badge_font(bold=True, size=13)
	pb = font_pill.getbbox(pill_txt)
	pw = (pb[2] - pb[0]) + 24
	ph = (pb[3] - pb[1]) + 12
	px2 = card_x2 - 25
	px1 = px2 - pw
	py1 = card_y1 + 22
	draw.rounded_rectangle([px1, py1, px2, py1 + ph], radius=ph // 2, fill=(15, 23, 42, 255))
	draw.text(((px1 + px2) // 2, py1 + ph // 2), pill_txt, fill=(255, 255, 255), font=font_pill, anchor="mm")

	# Left punch slot indicator
	slot_box_w, slot_box_h = 90, 24
	draw.rounded_rectangle([card_x1 + 25, card_y1 + 22, card_x1 + 25 + slot_box_w, card_y1 + 22 + slot_box_h], radius=8, fill=(255, 255, 255, 240))

	# Header: "CONNECTFEST 2027"
	font_title = _get_badge_font(bold=True, size=46)
	draw.text((card_x1 + 25, card_y1 + 72), "CONNECTFEST 2027", fill=(255, 255, 255), font=font_title)

	# Subtitle: "Grand Expo - 20 & 21 Feb 2027"
	font_sub = _get_badge_font(bold=False, size=18)
	draw.text((card_x1 + 26, card_y1 + 130), "Grand Expo - 20 & 21 Feb 2027", fill=(255, 255, 255), font=font_sub)

	# Dynamic Attendee Info:
	# NAME
	font_lbl = _get_badge_font(bold=True, size=11)
	draw.text((card_x1 + 26, card_y1 + 185), "NAME", fill=(241, 245, 249), font=font_lbl)

	font_name = _get_badge_font(bold=True, size=24)
	display_name = full_name.upper()
	while font_name.getbbox(display_name)[2] > (card_x2 - 40):
		break
	draw.text((card_x1 + 26, card_y1 + 204), display_name, fill=(255, 255, 255), font=font_name)

	# VENUE
	draw.text((card_x1 + 26, card_y1 + 248), "VENUE", fill=(241, 245, 249), font=font_lbl)
	font_venue = _get_badge_font(bold=True, size=16)
	draw.text((card_x1 + 26, card_y1 + 267), venue_text, fill=(255, 255, 255), font=font_venue)

	# Giant Role Title: "CNR MEMBER"
	font_role = _get_badge_font(bold=True, size=60)
	draw.text((card_x1 + 26, card_y1 + 312), role_header, fill=(255, 255, 255), font=font_role)

	# ----------------- Lower White Section -----------------
	white_top = card_y1 + red_h

	# Pass Code
	font_code = _get_badge_font(bold=True, size=17)
	draw.text((W // 2, white_top + 26), pass_code, fill=(30, 41, 59), font=font_code, anchor="mm")

	# Gate Pass QR Code
	qr = pyqrcode.create(qr_payload)
	matrix = qr.code
	qr_border = 2
	scale = 4
	qr_raw_size = (len(matrix) + 2 * qr_border) * scale
	qr_img = Image.new("RGB", (qr_raw_size, qr_raw_size), "white")
	qr_draw = ImageDraw.Draw(qr_img)
	for r_idx, row in enumerate(matrix):
		for c_idx, val in enumerate(row):
			if val == 1:
				x0 = (c_idx + qr_border) * scale
				y0 = (r_idx + qr_border) * scale
				qr_draw.rectangle([x0, y0, x0 + scale, y0 + scale], fill="black")

	qr_size = 140
	qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.NEAREST)
	qr_x = W // 2 - qr_size // 2
	qr_y = white_top + 48

	# Rounded QR container with subtle border
	draw.rounded_rectangle([qr_x - 6, qr_y - 6, qr_x + qr_size + 5, qr_y + qr_size + 5], radius=10, fill=(255, 255, 255), outline=(203, 213, 225), width=1)
	im.paste(qr_img, (qr_x, qr_y))

	# "SCAN AT ENTRY"
	font_scan = _get_badge_font(bold=True, size=11)
	draw.text((W // 2, qr_y + qr_size + 14), "SCAN AT ENTRY", fill=(71, 85, 105), font=font_scan, anchor="mm")

	# Divider line above footer
	draw.line([card_x1 + 30, qr_y + qr_size + 28, card_x2 - 30, qr_y + qr_size + 28], fill=(226, 232, 240), width=1)

	# Footer contacts
	font_cname = _get_badge_font(bold=True, size=13)
	font_cphone = _get_badge_font(bold=True, size=12)

	draw.text((card_x1 + 45, white_top + 258), "Mr. S. Mariappan", fill=(15, 23, 42), font=font_cname)
	draw.text((card_x1 + 45, white_top + 276), "+91 8825668054", fill=(220, 38, 38), font=font_cphone)

	draw.text((card_x2 - 180, white_top + 258), "Mr. R. Manooj", fill=(15, 23, 42), font=font_cname)
	draw.text((card_x2 - 180, white_top + 276), "+91 8939291011", fill=(220, 38, 38), font=font_cphone)

	# Outer card subtle outline
	draw.rounded_rectangle([card_x1, card_y1, card_x2, card_y2], radius=card_r, outline=(203, 213, 225), width=2)

	# Save PNG file to /public/files/event_passes/
	safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", pass_code)
	filename = f"ConnectFest_Event_{safe_code}.png"

	passes_dir = frappe.get_site_path("public", "files", "event_passes")
	os.makedirs(passes_dir, exist_ok=True)
	filepath = os.path.join(passes_dir, filename)

	im.save(filepath, format="PNG", optimize=True)
	rel_url = f"/files/event_passes/{filename}"

	# Store in Frappe DB record if column exists
	docname = get_val("name") or get_val("registration_id")
	if docname and hasattr(frappe, "db") and frappe.db:
		try:
			if frappe.db.has_column("ConnectFest BNI Event Registration", "event_pass_url"):
				frappe.db.set_value("ConnectFest BNI Event Registration", docname, "event_pass_url", rel_url)
				frappe.db.commit()
		except Exception:
			pass

	return rel_url


def download_event_badge(code=None, registration_id=None, mobile_no=None, email=None, **kwargs):
	"""Public / Authenticated API to download event badge pass image."""
	data = extract_request_data(kwargs)
	pass_code = (code or data.get("code") or data.get("pass_code") or "").strip()
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	mob = (mobile_no or data.get("mobile_no") or "").strip()
	em = (email or data.get("email") or "").strip().lower()

	rec = get_event_badge_data(pass_code=pass_code, registration_id=reg_id, mobile_no=mob, email=em)
	if not rec:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": "Event registration not found"}

	badge_rel_path = generate_event_badge_image(rec)
	if not badge_rel_path:
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Failed to generate event badge image"}

	fname = os.path.basename(badge_rel_path)
	full_path = frappe.get_site_path("public", "files", "event_passes", fname)
	if not os.path.exists(full_path):
		full_path = os.path.join(frappe.get_site_path("public"), badge_rel_path.lstrip("/"))

	if os.path.exists(full_path):
		with open(full_path, "rb") as f:
			png_bytes = f.read()
		frappe.local.response.filename = fname
		frappe.local.response.filecontent = png_bytes
		frappe.local.response.type = "download"
		frappe.local.response.content_type = "image/png"
		return

	return {"status": "success", "url": format_full_url(badge_rel_path)}
