import re
import frappe
from frappe.utils import cint
from connectfest.services.common import extract_request_data, parse_boolean, safe_str


def normalize_phone_number(raw_phone):
	"""
	Strictly normalizes mobile numbers regardless of formatting:
	- Removes spaces, hyphens, brackets, special characters
	- Removes country code prefixes (+91, 091, 91 for Indian numbers, or leading 0)
	- Returns exact 10-digit clean string
	"""
	if not raw_phone:
		return ""
	# Strip non-digits
	digits = re.sub(r"\D", "", str(raw_phone).strip())

	# Strip leading zeros
	while digits.startswith("0") and len(digits) > 10:
		digits = digits[1:]

	# If starts with country code 91 and has 12 digits
	if len(digits) == 12 and digits.startswith("91"):
		digits = digits[2:]
	elif len(digits) > 10 and digits.startswith("91"):
		digits = digits[2:]

	# Take last 10 digits if more than 10 digits
	if len(digits) > 10:
		digits = digits[-10:]

	return digits


def is_valid_bni_member(mobile_no):
	"""
	Checks whether a given mobile number belongs to a registered BNI Member.
	Matches exact normalized 10-digit number.
	Returns: (is_valid: bool, member_doc: dict or None)
	"""
	clean_mob = normalize_phone_number(mobile_no)
	if not clean_mob or len(clean_mob) != 10:
		return False, None

	# Query by exact match or right-10 digit match
	members = frappe.db.sql("""
		SELECT name, mobile_no, member_name
		FROM `tabConnectFest BNI Member`
		WHERE mobile_no = %(mob)s OR RIGHT(mobile_no, 10) = %(mob)s
		LIMIT 1
	""", {"mob": clean_mob}, as_dict=True)

	if members:
		return True, members[0]
	return False, None


def admin_create_bni_member(**kwargs):
	"""
	Admin API: Add single or bulk BNI Members (Name and Mobile Number).
	Accepts:
	1. Direct array of objects under 'members' or 'items':
	   {
	       "members": [
	           {"mobile_no": "9876543210", "member_name": "Karthik"},
	           {"mobile_no": "9876543211", "member_name": "Priya"}
	       ]
	   }
	2. Direct JSON list at root:
	   [
	       {"mobile_no": "9876543210", "member_name": "Karthik"},
	       {"mobile_no": "9876543211", "member_name": "Priya"}
	   ]
	3. Single member:
	   {"mobile_no": "9876543210", "member_name": "Karthik"}
	4. List of numbers with shared member_name:
	   {"mobile_numbers": ["9876543210", "9876543211"], "member_name": "Batch Name"}
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("BNI member management")
		if err:
			return err

	data = extract_request_data(kwargs)

	# Handle case where request payload is directly a list: e.g. [{"mobile_no": ...}, ...]
	if isinstance(data, list):
		raw_items = data
		default_name = ""
	else:
		raw_items = (
			data.get("members")
			or data.get("items")
			or data.get("data")
			or kwargs.get("members")
			or kwargs.get("items")
		)
		default_name = safe_str(data.get("member_name") or data.get("name") or kwargs.get("member_name") or "")

	items_to_process = []  # list of tuples: (clean_mobile, name)
	seen_mobs = set()

	if raw_items and isinstance(raw_items, (list, tuple)):
		for it in raw_items:
			if isinstance(it, dict):
				m_raw = it.get("mobile_no") or it.get("mobile") or it.get("phone") or it.get("number") or it.get("name")
				n_val = safe_str(it.get("member_name") or it.get("name") or default_name)
			else:
				m_raw = it
				n_val = default_name
			c_mob = normalize_phone_number(m_raw)
			if c_mob and c_mob not in seen_mobs:
				seen_mobs.add(c_mob)
				items_to_process.append((c_mob, n_val))
	else:
		raw_mob = data.get("mobile_no") or data.get("mobile") or data.get("phone") or kwargs.get("mobile_no")
		raw_mobs = data.get("mobile_numbers") or data.get("numbers") or kwargs.get("mobile_numbers")

		if raw_mobs and isinstance(raw_mobs, (list, tuple)):
			for m in raw_mobs:
				if isinstance(m, dict):
					m_raw = m.get("mobile_no") or m.get("mobile") or m.get("phone") or m.get("number")
					n_val = safe_str(m.get("member_name") or m.get("name") or default_name)
				else:
					m_raw = m
					n_val = default_name
				c_mob = normalize_phone_number(m_raw)
				if c_mob and c_mob not in seen_mobs:
					seen_mobs.add(c_mob)
					items_to_process.append((c_mob, n_val))
		elif raw_mob:
			for part in str(raw_mob).split(","):
				c_mob = normalize_phone_number(part)
				if c_mob and c_mob not in seen_mobs:
					seen_mobs.add(c_mob)
					items_to_process.append((c_mob, default_name))

	if not items_to_process:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Valid 10-digit Mobile Number (mobile_no) or members list is required!",
		}

	created = []
	updated = []

	for mob, m_name in items_to_process:
		existing = frappe.db.get_value("ConnectFest BNI Member", {"mobile_no": mob}, "name")
		if existing:
			doc = frappe.get_doc("ConnectFest BNI Member", existing)
			if m_name:
				doc.member_name = m_name
			doc.save(ignore_permissions=True)
			updated.append({"mobile_no": mob, "member_name": doc.member_name})
		else:
			doc = frappe.get_doc({
				"doctype": "ConnectFest BNI Member",
				"mobile_no": mob,
				"member_name": m_name,
			})
			doc.insert(ignore_permissions=True)
			created.append({"mobile_no": mob, "member_name": doc.member_name})

	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Successfully processed {len(items_to_process)} BNI Member(s) (Created: {len(created)}, Updated: {len(updated)})",
		"created": created,
		"updated": updated,
		"created_count": len(created),
		"updated_count": len(updated),
		"total_processed": len(items_to_process),
	}


def admin_get_bni_members(**kwargs):
	"""
	Admin API: List all BNI Members with search on name/number and pagination.
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("BNI members view")
		if err:
			return err

	data = extract_request_data(kwargs)
	search = (data.get("search") or data.get("query") or data.get("q") or kwargs.get("search") or "").strip().lower()
	raw_mob_filter = data.get("mobile_no") or data.get("mobile") or data.get("phone") or kwargs.get("mobile_no") or kwargs.get("phone") or ""
	mobile_filter = normalize_phone_number(raw_mob_filter) if raw_mob_filter else ""

	raw_page = data.get("page") or kwargs.get("page") or 1
	raw_limit = data.get("limit") or kwargs.get("limit") or 50
	page = max(1, cint(raw_page))
	limit = cint(raw_limit) if raw_limit and str(raw_limit).lower() != "all" else 0

	members = frappe.get_all(
		"ConnectFest BNI Member",
		fields=["name", "mobile_no", "member_name", "creation", "modified"],
		order_by="creation DESC",
		limit=5000,
	)

	filtered = []
	for m in members:
		if mobile_filter:
			m_num = normalize_phone_number(m.mobile_no)
			if mobile_filter not in m_num and str(raw_mob_filter).strip() not in (m.mobile_no or ""):
				continue
		if search:
			clean_s = normalize_phone_number(search)
			m_mob = search in (m.mobile_no or "").lower() or (clean_s and clean_s in (m.mobile_no or ""))
			m_name = search in (m.member_name or "").lower()
			if not (m_mob or m_name):
				continue
		filtered.append(m)

	total_count = len(filtered)
	if limit > 0:
		start = (page - 1) * limit
		end = start + limit
		paginated = filtered[start:end]
		tot_pages = max(1, (total_count + limit - 1) // limit)
		has_next = page < tot_pages
		has_prev = page > 1
	else:
		paginated = filtered
		tot_pages = 1
		has_next = False
		has_prev = False

	return {
		"status": "success",
		"has_next": has_next,
		"has_prev": has_prev,
		"summary": {
			"total_bni_members": total_count,
			"page": page,
			"page_size": limit if limit > 0 else total_count,
			"total_pages": tot_pages,
			"has_next": has_next,
			"has_prev": has_prev,
		},
		"members": paginated,
	}


def admin_get_bni_member(mobile_no=None, **kwargs):
	"""
	Admin API: Get a single BNI member details by mobile_no or id.
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("BNI member details")
		if err:
			return err

	data = extract_request_data(kwargs)
	raw_mob = mobile_no or data.get("mobile_no") or data.get("mobile") or data.get("phone") or kwargs.get("mobile_no")
	clean_mob = normalize_phone_number(raw_mob)

	member = None
	if clean_mob:
		member = frappe.db.get_value(
			"ConnectFest BNI Member",
			{"mobile_no": clean_mob},
			["name", "mobile_no", "member_name", "creation", "modified"],
			as_dict=True,
		)
	if not member and raw_mob:
		if frappe.db.exists("ConnectFest BNI Member", str(raw_mob).strip()):
			member = frappe.get_doc("ConnectFest BNI Member", str(raw_mob).strip()).as_dict()

	if not member:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": f"BNI Member with mobile number '{raw_mob}' not found",
		}

	return {
		"status": "success",
		"member": member,
	}


def admin_update_bni_member(mobile_no=None, **kwargs):
	"""
	Admin API: Update an existing BNI member (Name or Mobile number).
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("BNI member update")
		if err:
			return err

	data = extract_request_data(kwargs)
	raw_mob = mobile_no or data.get("mobile_no") or data.get("mobile") or data.get("phone") or data.get("id")
	clean_mob = normalize_phone_number(raw_mob)

	doc_name = None
	if clean_mob and frappe.db.exists("ConnectFest BNI Member", clean_mob):
		doc_name = clean_mob
	elif raw_mob and frappe.db.exists("ConnectFest BNI Member", str(raw_mob).strip()):
		doc_name = str(raw_mob).strip()

	if not doc_name:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": f"BNI Member '{raw_mob}' not found",
		}

	doc = frappe.get_doc("ConnectFest BNI Member", doc_name)
	new_name = data.get("member_name") if "member_name" in data else data.get("name") if "name" in data else None
	if new_name is not None:
		doc.member_name = safe_str(new_name)

	# Support updating / renaming to a new mobile number
	new_mob_raw = data.get("new_mobile_no") or data.get("new_mobile") or data.get("new_phone")
	if new_mob_raw:
		new_mob = normalize_phone_number(new_mob_raw)
		if new_mob and len(new_mob) == 10 and new_mob != doc.mobile_no:
			if frappe.db.exists("ConnectFest BNI Member", new_mob):
				frappe.local.response["http_status_code"] = 409
				return {"status": "error", "message": f"Mobile number '{new_mob}' is already assigned to another BNI Member"}
			doc.save(ignore_permissions=True)
			frappe.rename_doc("ConnectFest BNI Member", doc.name, new_mob, force=True)
			doc = frappe.get_doc("ConnectFest BNI Member", new_mob)

	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": f"BNI Member '{doc.mobile_no}' updated successfully",
		"member": doc.as_dict(),
	}


def admin_delete_bni_member(mobile_no=None, mobile_numbers=None, **kwargs):
	"""
	Admin API: Delete single or bulk BNI members.
	Accepts:
	- mobile_no: Single string, comma-separated string, or list
	- mobile_numbers: List of mobile numbers (e.g. ["9876543210", "9876543211"])
	- numbers / items / ids: Aliases for bulk list
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("BNI member deletion")
		if err:
			return err

	data = extract_request_data(kwargs)
	raw_mob = mobile_no or data.get("mobile_no") or data.get("mobile") or data.get("phone")
	raw_mobs = (
		mobile_numbers
		or data.get("mobile_numbers")
		or data.get("numbers")
		or data.get("items")
		or data.get("ids")
		or data.get("members")
		or kwargs.get("mobile_numbers")
		or kwargs.get("numbers")
	)

	target_numbers = []
	if raw_mobs and isinstance(raw_mobs, (list, tuple)):
		for m in raw_mobs:
			if isinstance(m, dict):
				val = m.get("mobile_no") or m.get("mobile") or m.get("phone") or m.get("name")
			else:
				val = m
			c = normalize_phone_number(val)
			if c and c not in target_numbers:
				target_numbers.append(c)
	elif isinstance(raw_mob, (list, tuple)):
		for m in raw_mob:
			if isinstance(m, dict):
				val = m.get("mobile_no") or m.get("mobile") or m.get("phone") or m.get("name")
			else:
				val = m
			c = normalize_phone_number(val)
			if c and c not in target_numbers:
				target_numbers.append(c)
	elif raw_mob:
		for part in str(raw_mob).split(","):
			c = normalize_phone_number(part)
			if c and c not in target_numbers:
				target_numbers.append(c)

	if not target_numbers:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Target mobile_no or mobile_numbers list is required for deletion.",
		}

	deleted = []
	not_found = []
	for mob in target_numbers:
		doc_name = None
		if frappe.db.exists("ConnectFest BNI Member", mob):
			doc_name = mob
		else:
			match = frappe.db.get_value("ConnectFest BNI Member", {"mobile_no": mob}, "name")
			if match:
				doc_name = match

		if doc_name:
			frappe.delete_doc("ConnectFest BNI Member", doc_name, ignore_permissions=True, force=True)
			deleted.append(mob)
		else:
			not_found.append(mob)

	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Successfully deleted {len(deleted)} BNI Member(s)",
		"deleted_count": len(deleted),
		"deleted_members": deleted,
		"not_found": not_found,
	}


def admin_bulk_delete_bni_members(**kwargs):
	"""
	Admin API: Dedicated endpoint for bulk deletion of BNI members.
	"""
	return admin_delete_bni_member(**kwargs)


def is_crn_bni_restriction_enabled():
	"""
	Returns whether Admin has enabled strict BNI restriction for CRN 1 (Corner) stalls.
	Defaults to 1 (True).
	"""
	try:
		val = frappe.db.get_single_value("ConnectFest Settings", "crn_bni_only")
		if val is None or str(val).strip() == "":
			return True
		return bool(cint(val))
	except Exception:
		return True


def admin_toggle_crn_bni_restriction(enable=None, **kwargs):
	"""
	Admin API: Toggle or set CRN BNI member restriction on/off.
	- enable: 1 (ON - strictly verified against BNI Member doctype for CRN stalls)
	          0 (OFF - normal flow, anyone can book/register)
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("CRN BNI restriction controls")
		if err:
			return err

	data = extract_request_data(kwargs)
	raw_val = enable if enable is not None else data.get("enable") if "enable" in data else data.get("is_enabled") if "is_enabled" in data else kwargs.get("enable")

	curr = is_crn_bni_restriction_enabled()
	if raw_val is None:
		new_val = 0 if curr else 1
	else:
		new_val = 1 if (parse_boolean(raw_val) or str(raw_val).strip().lower() in ("1", "true", "yes", "on")) else 0

	frappe.db.set_single_value("ConnectFest Settings", "crn_bni_only", new_val)
	frappe.db.commit()

	try:
		from connectfest.services.stalls import invalidate_stalls_cache
		invalidate_stalls_cache()
	except Exception:
		pass

	return {
		"status": "success",
		"message": f"CRN BNI restriction successfully {'enabled' if new_val else 'disabled'}",
		"crn_bni_only": new_val,
		"is_enabled": bool(new_val),
	}


# Aliases
create_bni_member = admin_create_bni_member
admin_bulk_create_bni_members = admin_create_bni_member
bulk_create_bni_members = admin_create_bni_member
get_bni_members = admin_get_bni_members
get_bni_member = admin_get_bni_member
update_bni_member = admin_update_bni_member
admin_edit_bni_member = admin_update_bni_member
edit_bni_member = admin_update_bni_member
delete_bni_member = admin_delete_bni_member
bulk_delete_bni_members = admin_bulk_delete_bni_members
toggle_crn_bni_restriction = admin_toggle_crn_bni_restriction
admin_set_crn_bni_restriction = admin_toggle_crn_bni_restriction
