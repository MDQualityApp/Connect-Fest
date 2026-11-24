import os
import re
import io
import base64
import urllib.parse
import frappe
from frappe.utils import now_datetime, cint, flt
from connectfest.services.common import extract_request_data, handle_api_errors, format_full_url, safe_str
from connectfest.services.visitor_badge import _load_badge_image, _get_badge_font

_TEMPLATES_DIR = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
	"public",
	"templates"
)


def get_exhibitor_badge_data(pass_code=None, registration_id=None, stall_number=None, mobile_no=None, email=None):
	"""Fetches exhibitor registration record for badge generation."""
	conds = []
	vals = {}

	clean_code = (pass_code or "").strip()
	clean_reg = (registration_id or "").strip()
	clean_stall = (stall_number or "").strip()
	clean_mob = (mobile_no or "").strip()
	clean_em = (email or "").strip().lower()

	if clean_code:
		conds.append("(pass_code = %(code)s OR pass_2_pass_code = %(code)s OR registration_id = %(code)s OR name = %(code)s)")
		vals["code"] = clean_code
	if clean_reg:
		conds.append("(registration_id = %(reg)s OR name = %(reg)s)")
		vals["reg"] = clean_reg
	if clean_stall:
		conds.append("stall_number = %(stall)s")
		vals["stall"] = clean_stall
	if clean_mob:
		conds.append("(mobile_no = %(mob)s OR pass_2_mobile = %(mob)s)")
		vals["mob"] = clean_mob
	if clean_em:
		conds.append("(email = %(em)s OR pass_2_email = %(em)s)")
		vals["em"] = clean_em

	if not conds:
		return None

	where_clause = " OR ".join(conds)
	records = frappe.db.sql(f"""
		SELECT name, registration_id, full_name, company_name, company_logo, location,
		       mobile_no, email, is_bni_member, is_cnr_member, region_name, chapter_name,
		       industry, segment, stall_number, stall_category, amount, payment_status,
		       payment_mode, payment_date, status, creation, pass_code, qr_payload,
		       checked_in, checked_in_at, checked_in_by, pass_1_image, exhibitor_pass_url,
		       pass_2_name, pass_2_image, pass_2_mobile, pass_2_email,
		       pass_2_pass_code, pass_2_qr_payload, pass_2_checked_in, pass_2_checked_in_at,
		       pass_2_checked_in_by, pass_2_pass_url
		FROM `tabConnectFest Registration`
		WHERE {where_clause}
		ORDER BY creation DESC
		LIMIT 1
	""", vals, as_dict=True)

	if records:
		return records[0]
	return None


def generate_exhibitor_badge_image(data, pass_index=1):
	"""
	Generates an ultra-high-definition, official Exhibitor Pass Badge image:
	- Base template: exhibitor_badge_template.png (ConnectFest + BNI red header, black body, EXHIBITOR banner, STALL & VENUE footer)
	- Upper center section:
	    * Attendee Photo / Company Logo or Monogram circle avatar with anti-aliasing
	    * Attendee Full Name (auto-scaled crisp white)
	    * Company Name (auto-scaled light silver)
	    * Role Pill ('PRIMARY EXHIBITOR' or 'CO-EXHIBITOR')
	    * Location / Chapter subtitle
	- Lower left section:
	    * Stall Number (large high-contrast bold white under 'STALL')
	    * Stall Category / Tier (gold)
	- Lower right section:
	    * High-contrast Gate Pass QR code (pyqrcode) matching entrance scanner
	    * Pass Code text above QR
	    * 'SCAN AT ENTRY' label below QR
	Saves PNG image to /public/files/exhibitor_badges/ and returns relative URL.
	Also updates 'exhibitor_pass_url' or 'pass_2_pass_url' in Frappe DB.
	"""
	from PIL import Image, ImageDraw, ImageOps
	import pyqrcode

	template_path = os.path.join(_TEMPLATES_DIR, "exhibitor_badge_template.png")
	if not os.path.exists(template_path):
		# Fallback path if template moved
		template_path = os.path.join(
			os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
			"public",
			"templates",
			"exhibitor_badge_template.png"
		)
		if not os.path.exists(template_path):
			return ""

	get_val = getattr(data, "get", None) or (lambda k, d=None: getattr(data, k, d))

	p_idx = cint(pass_index or 1)
	is_cnr = cint(get_val("is_cnr_member") or 0)
	if p_idx == 2:
		# Co-Exhibitor Pass 2
		full_name = (get_val("pass_2_name") or "Co-Exhibitor").strip()
		pass_code = (get_val("pass_2_pass_code") or f"{get_val('registration_id') or get_val('name')}-P2").strip()
		user_img_source = get_val("pass_2_image") or get_val("company_logo") or ""
		role_label = "CNR CO-EXHIBITOR" if is_cnr else "CO-EXHIBITOR (BOOTH STAFF)"
		url_db_col = "pass_2_pass_url"
	else:
		# Primary Exhibitor Pass 1
		full_name = (get_val("full_name") or "Valued Exhibitor").strip()
		pass_code = (get_val("pass_code") or get_val("registration_id") or get_val("name") or "CF-EXHIBITOR").strip()
		user_img_source = (
			get_val("pass_1_image")
			or get_val("company_logo")
			or get_val("visitor_image")
			or get_val("image")
			or get_val("photo")
			or get_val("user_image")
			or get_val("avatar")
			or ""
		)
		role_label = "CNR EXHIBITOR" if is_cnr else "PRIMARY EXHIBITOR"
		url_db_col = "exhibitor_pass_url"

	# Avoid self-referencing already generated badges as source photo
	if user_img_source and ("exhibitor_badges" in str(user_img_source) or "ConnectFest_Exhibitor_" in str(user_img_source)):
		user_img_source = ""

	# Check User and Registration tables if photo is missing
	user_email = (get_val("email") or "").strip().lower()
	user_mobile = (get_val("mobile_no") or "").strip()
	reg_id_val = get_val("registration_id") or get_val("name")
	if not user_img_source and hasattr(frappe, "db") and frappe.db:
		try:
			# 1. Check User table
			if user_email:
				u_img = frappe.db.get_value("User", user_email, "user_image")
				if u_img and "exhibitor_badges" not in str(u_img) and "visitor_badges" not in str(u_img):
					user_img_source = u_img

			# 2. Check ConnectFest Registration table directly
			if not user_img_source and reg_id_val:
				db_img = (
					frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id_val}, "pass_1_image")
					or frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id_val}, "company_logo")
					or frappe.db.get_value("ConnectFest Registration", reg_id_val, "pass_1_image")
					or frappe.db.get_value("ConnectFest Registration", reg_id_val, "company_logo")
				)
				if db_img and "exhibitor_badges" not in str(db_img) and "visitor_badges" not in str(db_img):
					user_img_source = db_img

			# 3. Check ConnectFest Visitor Registration by email / mobile
			if not user_img_source and user_email:
				v_img = frappe.db.get_value("ConnectFest Visitor Registration", {"email": user_email}, "visitor_image")
				if v_img and "visitor_badges" not in str(v_img):
					user_img_source = v_img
			if not user_img_source and user_mobile:
				v_img = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": user_mobile}, "visitor_image")
				if v_img and "visitor_badges" not in str(v_img):
					user_img_source = v_img
		except Exception:
			pass

	company_name = (get_val("company_name") or get_val("company") or "").strip()
	stall_number = (get_val("stall_number") or get_val("stall_no") or "PENDING").strip().upper()
	stall_category = (get_val("stall_category") or get_val("tier") or "STANDARD").strip().upper()
	location = (get_val("location") or "").strip()
	chapter_name = (get_val("chapter_name") or "").strip()

	raw_qr = get_val("pass_2_qr_payload") if p_idx == 2 else get_val("qr_payload")
	qr_payload = str(raw_qr or pass_code).strip()
	if not qr_payload or "CONNECTFEST" not in qr_payload:
		qr_payload = pass_code

	# Open base template
	base_im = Image.open(template_path).convert("RGBA")
	w, h = base_im.size
	center_x = 316

	draw = ImageDraw.Draw(base_im)

	# -------------------------------------------------------------------------
	# 1. Attendee Photo / Avatar / Logo Placement (Middle Area y=225..425)
	# -------------------------------------------------------------------------
	avatar_size = 160
	pos_x = center_x - avatar_size // 2
	pos_y = 236

	user_logo_im = _load_badge_image(user_img_source) if user_img_source else None
	if user_logo_im:
		lw, lh = user_logo_im.size
		aspect = lw / float(lh)

		if aspect > 1.35:
			# Wide / Rectangular Landscape Logo: Place inside a clean rounded white showcase card
			card_w = min(260, avatar_size + 80)
			card_h = avatar_size - 14
			card_x = center_x - card_w // 2
			card_y = pos_y + 7

			draw.rounded_rectangle(
				[card_x, card_y, card_x + card_w, card_y + card_h],
				radius=14,
				fill=(255, 255, 255),
				outline=(220, 38, 38),
				width=2
			)

			# Proportional containment: 100% of the logo is visible with zero cropping
			pad_x, pad_y = 12, 10
			max_w = card_w - (pad_x * 2)
			max_h = card_h - (pad_y * 2)
			ratio = min(max_w / float(lw), max_h / float(lh))
			new_w = max(1, int(lw * ratio))
			new_h = max(1, int(lh * ratio))

			logo_resized = user_logo_im.resize((new_w, new_h), Image.Resampling.LANCZOS)
			lp_x = card_x + (card_w - new_w) // 2
			lp_y = card_y + (card_h - new_h) // 2

			mask = logo_resized.split()[3] if logo_resized.mode == "RGBA" else None
			base_im.paste(logo_resized, (lp_x, lp_y), mask=mask)
		else:
			# Square / Portrait / Moderate Landscape Photo or Logo: Clean Circular Avatar
			# 2x supersampling with safe inner bounds so ZERO text, corners, or faces are cropped
			scale_aa = 2
			hi_size = avatar_size * scale_aa

			# White circular background for logos, dark slate background for transparent icons
			circ_canvas = Image.new("RGBA", (hi_size, hi_size), (255, 255, 255, 255))
			diag_max = int(hi_size * 0.86)
			r = min(diag_max / ((lw ** 2 + lh ** 2) ** 0.5), 1.0)
			target_w = max(1, int(lw * r))
			target_h = max(1, int(lh * r))

			logo_scaled = user_logo_im.resize((target_w, target_h), Image.Resampling.LANCZOS)
			gx = (hi_size - target_w) // 2
			gy = (hi_size - target_h) // 2

			mask_l = logo_scaled.split()[3] if logo_scaled.mode == "RGBA" else None
			circ_canvas.paste(logo_scaled, (gx, gy), mask=mask_l)

			# Circular mask
			mask_hi = Image.new("L", (hi_size, hi_size), 0)
			mask_draw = ImageDraw.Draw(mask_hi)
			mask_draw.ellipse([0, 0, hi_size - 1, hi_size - 1], fill=255)
			circ_canvas.putalpha(mask_hi)

			photo_final = circ_canvas.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
			base_im.paste(photo_final, (pos_x, pos_y), mask=photo_final)

			# Red circular border ring
			draw.ellipse(
				[pos_x - 3, pos_y - 3, pos_x + avatar_size + 2, pos_y + avatar_size + 2],
				outline=(220, 38, 38),
				width=3
			)
	else:
		# Fallback: Official ConnectFest GOAT Mascot (matching Visitor Pass)
		# Scaled with safe vertical padding so horns, face, and suit are 100% visible with ZERO clipping
		goat_fitted = False
		vis_tpl = os.path.join(_TEMPLATES_DIR, "visitor_badge_template.jpg")
		if os.path.exists(vis_tpl):
			try:
				vis_im = Image.open(vis_tpl)
				# Full goat illustration including top of horns (y=105) and bottom suit (y=287)
				goat_crop = vis_im.crop((50, 105, 280, 287))
				gw, gh = goat_crop.size

				scale_aa = 2
				hi_size = avatar_size * scale_aa
				# Exact comic red background matching visitor template
				circ = Image.new("RGBA", (hi_size, hi_size), (188, 14, 38, 255))
				scale_fit = 230 / float(gh)
				tw = int(gw * scale_fit)
				th = int(gh * scale_fit)
				scaled_goat = goat_crop.resize((tw, th), Image.Resampling.LANCZOS)
				gx = (hi_size - tw) // 2
				gy = (hi_size - th) // 2
				circ.paste(scaled_goat, (gx, gy))

				mask_hi = Image.new("L", (hi_size, hi_size), 0)
				mask_draw = ImageDraw.Draw(mask_hi)
				mask_draw.ellipse([0, 0, hi_size - 1, hi_size - 1], fill=255)
				circ.putalpha(mask_hi)

				goat_circle = circ.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
				base_im.paste(goat_circle, (pos_x, pos_y), mask=goat_circle)
				goat_fitted = True
			except Exception:
				goat_fitted = False

		if not goat_fitted:
			avatar = Image.new("RGBA", (avatar_size, avatar_size), (30, 41, 59, 255))
			av_draw = ImageDraw.Draw(avatar)
			words = [w for w in full_name.split() if w]
			if len(words) >= 2:
				initials = f"{words[0][0]}{words[1][0]}".upper()
			elif words:
				initials = words[0][:2].upper()
			else:
				initials = "CF"
			f_init = _get_badge_font(bold=True, size=54)
			av_draw.text((avatar_size // 2, avatar_size // 2), initials, fill=(255, 255, 255), font=f_init, anchor="mm")
			mask = Image.new("L", (avatar_size, avatar_size), 0)
			mask_draw = ImageDraw.Draw(mask)
			mask_draw.ellipse([0, 0, avatar_size - 1, avatar_size - 1], fill=255)
			base_im.paste(avatar, (pos_x, pos_y), mask=mask)

		# Red circular border ring
		draw.ellipse(
			[pos_x - 3, pos_y - 3, pos_x + avatar_size + 2, pos_y + avatar_size + 2],
			outline=(220, 38, 38),
			width=3
		)

	# -------------------------------------------------------------------------
	# 2. Attendee Full Name (Auto-scaled)
	# -------------------------------------------------------------------------
	display_name = full_name.upper()
	name_font_size = 25
	font_name = _get_badge_font(bold=True, size=name_font_size)
	while name_font_size > 14:
		bbox = font_name.getbbox(display_name)
		if (bbox[2] - bbox[0]) <= 500:
			break
		name_font_size -= 1
		font_name = _get_badge_font(bold=True, size=name_font_size)

	draw.text((center_x, 436), display_name, fill=(255, 255, 255), font=font_name, anchor="mm")

	# -------------------------------------------------------------------------
	# 3. Company Name (Auto-scaled)
	# -------------------------------------------------------------------------
	display_company = company_name if company_name else "-"
	comp_font_size = 17
	font_company = _get_badge_font(bold=True, size=comp_font_size)
	while comp_font_size > 11:
		bbox = font_company.getbbox(display_company)
		if (bbox[2] - bbox[0]) <= 480:
			break
		comp_font_size -= 1
		font_company = _get_badge_font(bold=True, size=comp_font_size)

	draw.text((center_x, 470), display_company, fill=(203, 213, 225), font=font_company, anchor="mm")

	# -------------------------------------------------------------------------
	# 4. Role Pill ('PRIMARY EXHIBITOR' or 'CO-EXHIBITOR')
	# -------------------------------------------------------------------------
	font_pill = _get_badge_font(bold=True, size=11)
	pill_bbox = font_pill.getbbox(role_label)
	pw = (pill_bbox[2] - pill_bbox[0]) + 24
	ph = (pill_bbox[3] - pill_bbox[1]) + 12
	px1 = center_x - pw // 2
	py1 = 498
	draw.rounded_rectangle([px1, py1, px1 + pw, py1 + ph], radius=10, fill=(185, 28, 28))
	draw.text((center_x, py1 + ph // 2), role_label, fill=(255, 255, 255), font=font_pill, anchor="mm")

	# -------------------------------------------------------------------------
	# 5. Location / Chapter Subtitle
	# -------------------------------------------------------------------------
	sub_parts = []
	if location:
		sub_parts.append(location)
	if chapter_name and chapter_name != "BNI":
		sub_parts.append(f"BNI {chapter_name}")
	sub_text = " • ".join(sub_parts) if sub_parts else "Chennai • Tamil Nadu"

	font_sub = _get_badge_font(bold=False, size=12.5)
	sub_bbox = font_sub.getbbox(sub_text)
	if (sub_bbox[2] - sub_bbox[0]) > 480:
		sub_text = sub_text[:45] + "..."
	draw.text((center_x, 538), sub_text, fill=(148, 163, 184), font=font_sub, anchor="mm")

	# -------------------------------------------------------------------------
	# 6. Lower Left Section: Stall Number and Category
	# -------------------------------------------------------------------------
	# Stall Number (Large bold white below 'STALL' label)
	stall_font_size = 36
	font_stall = _get_badge_font(bold=True, size=stall_font_size)
	display_stall = stall_number if stall_number else "A-01"
	draw.text((71, 768), display_stall, fill=(255, 255, 255), font=font_stall)

	# Tier & Dimensions (Gold)
	cat_text = f"{stall_category} TIER"
	font_cat = _get_badge_font(bold=True, size=12.5)
	draw.text((71, 818), cat_text, fill=(245, 158, 11), font=font_cat)

	# -------------------------------------------------------------------------
	# 7. Lower Right Section: Gate Pass QR Code (pyqrcode) & Pass Code
	# -------------------------------------------------------------------------
	qr = pyqrcode.create(qr_payload)
	matrix = qr.code
	qr_border = 2
	scale = 3
	qr_raw_size = (len(matrix) + 2 * qr_border) * scale
	qr_img = Image.new("RGB", (qr_raw_size, qr_raw_size), "white")
	qr_draw = ImageDraw.Draw(qr_img)
	for r_idx, row in enumerate(matrix):
		for c_idx, val in enumerate(row):
			if val == 1:
				x0 = (c_idx + qr_border) * scale
				y0 = (r_idx + qr_border) * scale
				qr_draw.rectangle([x0, y0, x0 + scale, y0 + scale], fill="black")

	target_qr_size = 114
	qr_img = qr_img.resize((target_qr_size, target_qr_size), Image.Resampling.NEAREST)

	qr_center_x = 480
	qr_x = qr_center_x - target_qr_size // 2
	qr_y = 770

	# QR background container with border
	draw.rounded_rectangle(
		[qr_x - 4, qr_y - 4, qr_x + target_qr_size + 3, qr_y + target_qr_size + 3],
		radius=8,
		fill=(255, 255, 255),
		outline=(203, 213, 225),
		width=1
	)
	base_im.paste(qr_img, (qr_x, qr_y))

	# Pass code text above QR
	font_code = _get_badge_font(bold=True, size=11)
	draw.text((qr_center_x, 752), pass_code, fill=(226, 232, 240), font=font_code, anchor="mm")

	# SCAN AT ENTRY below QR
	font_scan = _get_badge_font(bold=True, size=9.5)
	draw.text((qr_center_x, 902), "SCAN AT ENTRY", fill=(248, 113, 113), font=font_scan, anchor="mm")

	# -------------------------------------------------------------------------
	# 8. Save image to public files
	# -------------------------------------------------------------------------
	safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", pass_code)
	if p_idx == 2 and not safe_code.endswith("_P2"):
		safe_code = f"{safe_code}_P2"
	filename = f"ConnectFest_Exhibitor_{safe_code}.png"

	badges_dir = frappe.get_site_path("public", "files", "exhibitor_badges")
	os.makedirs(badges_dir, exist_ok=True)
	filepath = os.path.join(badges_dir, filename)

	base_im.save(filepath, format="PNG", optimize=True)
	rel_url = f"/files/exhibitor_badges/{filename}"

	# -------------------------------------------------------------------------
	# 9. Store relative URL in Frappe Registration record if exists
	# -------------------------------------------------------------------------
	docname = get_val("name") or get_val("registration_id")
	if docname and hasattr(frappe, "db") and frappe.db:
		try:
			if frappe.db.has_column("ConnectFest Registration", url_db_col):
				frappe.db.set_value("ConnectFest Registration", docname, url_db_col, rel_url)
				frappe.db.commit()
		except Exception:
			pass

	return rel_url


def download_exhibitor_badge(code=None, registration_id=None, stall_number=None, pass_index=1, **kwargs):
	"""
	Public / Authenticated Exhibitor Badge Download API.
	Returns binary PNG file directly.
	"""
	data = extract_request_data(kwargs)
	pass_code = (code or data.get("code") or data.get("pass_code") or "").strip()
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	stall_num = (stall_number or data.get("stall_number") or "").strip()
	p_idx = cint(pass_index or data.get("pass_index") or (2 if "-P2" in pass_code.upper() else 1))

	rec = get_exhibitor_badge_data(
		pass_code=pass_code,
		registration_id=reg_id,
		stall_number=stall_num
	)
	if not rec:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": "Exhibitor registration not found"}

	badge_rel_path = generate_exhibitor_badge_image(rec, pass_index=p_idx)
	if not badge_rel_path:
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Failed to generate exhibitor badge image"}

	fname = os.path.basename(badge_rel_path)
	full_path = frappe.get_site_path("public", "files", "exhibitor_badges", fname)
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
