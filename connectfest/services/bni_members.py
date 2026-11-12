import re
import frappe
from frappe.utils import cint, flt
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


def get_bni_verification_settings():
	"""
	Reads dynamic BNI verification settings from ConnectFest Settings:
	- crn_bni_only: Master toggle for CRN stall restriction (default True)
	- check_mobile: Validate BNI membership by Mobile Number (default True)
	- check_email: Validate BNI membership by Email Address (default True)
	Note: If both check_mobile and check_email are OFF (0), CRN restriction is automatically OFF!
	"""
	try:
		doc = frappe.get_cached_doc("ConnectFest Settings")
		raw_crn = getattr(doc, "crn_bni_only", 1)
		raw_mob = getattr(doc, "bni_check_mobile", 1)
		raw_email = getattr(doc, "bni_check_email", 1)

		crn_only = True if (raw_crn is None or str(raw_crn).strip() == "") else bool(cint(raw_crn))
		check_mob = True if (raw_mob is None or str(raw_mob).strip() == "") else bool(cint(raw_mob))
		check_email = True if (raw_email is None or str(raw_email).strip() == "") else bool(cint(raw_email))

		if not check_mob and not check_email:
			crn_only = False

		return {
			"crn_bni_only": crn_only,
			"check_mobile": check_mob,
			"check_email": check_email,
		}
	except Exception:
		return {
			"crn_bni_only": True,
			"check_mobile": True,
			"check_email": True,
		}


def clear_bni_member_cache():
	"""
	Clears the in-memory Redis cache for BNI member validation.
	Called whenever BNI members are added, updated, or deleted.
	"""
	try:
		frappe.cache().delete_keys("bni_v2:*")
	except Exception:
		pass


def is_valid_bni_member(mobile_no=None, email=None):
	"""
	High-Performance Optimized BNI membership verification:
	1. In-memory Redis Cache: Returns instant sub-millisecond answer if previously checked.
	2. Indexed DB Seek (O(1)):
	   - Uses exact Primary Key / indexed `mobile_no` / `email` lookups without wrapping
	     columns in SQL functions (avoiding full table scans even with 10k - 100k+ rows).
	Returns: (is_valid: bool, member_doc: dict or None)
	"""
	cfg = get_bni_verification_settings()
	check_mob = cfg["check_mobile"]
	check_email = cfg["check_email"]

	clean_mob = normalize_phone_number(mobile_no) if mobile_no else ""
	clean_email = str(email or "").strip().lower() if email and "@" in str(email) else ""

	if not check_mob and not check_email:
		return False, None

	# Filter inputs according to dynamic admin flags
	use_mob = clean_mob if (check_mob and clean_mob and len(clean_mob) >= 8) else ""
	use_email = clean_email if (check_email and clean_email) else ""

	# Check Redis Cache first (Microsecond response)
	cache_key = f"bni_v2:{use_mob}:{use_email}"
	try:
		cached = frappe.cache().get_value(cache_key)
		if cached is not None and isinstance(cached, dict):
			return cached.get("valid", False), cached.get("doc")
	except Exception:
		pass

	# Candidate phone numbers for index matching (exact matches utilize DB indexes directly)
	mob_candidates = [use_mob]
	if use_mob:
		mob_candidates.extend([f"+91{use_mob}", f"91{use_mob}", f"0{use_mob}"])
	mob_candidates = tuple(set(mob_candidates))

	members = []
	if check_mob and check_email:
		if use_mob and use_email:
			members = frappe.db.sql("""
				SELECT name, first_name, last_name, member_name, mobile_no, email, company_name, industry, chapter_name
				FROM `tabConnectFest BNI Member`
				WHERE (name IN %(candidates)s OR mobile_no IN %(candidates)s)
				  AND email = %(email)s
				LIMIT 1
			""", {"candidates": mob_candidates, "email": use_email}, as_dict=True)
		elif use_mob:
			members = frappe.db.sql("""
				SELECT name, first_name, last_name, member_name, mobile_no, email, company_name, industry, chapter_name
				FROM `tabConnectFest BNI Member`
				WHERE name IN %(candidates)s OR mobile_no IN %(candidates)s
				LIMIT 1
			""", {"candidates": mob_candidates}, as_dict=True)
		elif use_email:
			members = frappe.db.sql("""
				SELECT name, first_name, last_name, member_name, mobile_no, email, company_name, industry, chapter_name
				FROM `tabConnectFest BNI Member`
				WHERE email = %(email)s
				LIMIT 1
			""", {"email": use_email}, as_dict=True)
		else:
			return False, None
	elif check_mob:
		if not use_mob:
			return False, None
		members = frappe.db.sql("""
			SELECT name, first_name, last_name, member_name, mobile_no, email, company_name, industry, chapter_name
			FROM `tabConnectFest BNI Member`
			WHERE name IN %(candidates)s OR mobile_no IN %(candidates)s
			LIMIT 1
		""", {"candidates": mob_candidates}, as_dict=True)
	else:
		# Email Only Check
		if not use_email:
			return False, None
		members = frappe.db.sql("""
			SELECT name, first_name, last_name, member_name, mobile_no, email, company_name, industry, chapter_name
			FROM `tabConnectFest BNI Member`
			WHERE email = %(email)s
			LIMIT 1
		""", {"email": use_email}, as_dict=True)

	res_valid = bool(members)
	res_doc = members[0] if members else None

	# Save in Redis cache with 30 minutes TTL
	try:
		frappe.cache().set_value(cache_key, {"valid": res_valid, "doc": res_doc}, expires_in_sec=1800)
	except Exception:
		pass

	return res_valid, res_doc


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

	def _extract_member_dict(obj, fallback_name="", fallback_email="", fallback_company="", fallback_industry="", fallback_chapter=""):
		if not isinstance(obj, dict):
			c_mob = normalize_phone_number(obj)
			if not c_mob:
				return None
			return {
				"mobile_no": c_mob,
				"first_name": "",
				"last_name": "",
				"member_name": fallback_name,
				"email": fallback_email,
				"company_name": fallback_company,
				"industry": fallback_industry,
				"chapter_name": fallback_chapter,
			}
		m_raw = (
			obj.get("mobile_no")
			or obj.get("Phone Number")
			or obj.get("phone_number")
			or obj.get("phone")
			or obj.get("mobile")
			or obj.get("number")
		)
		c_mob = normalize_phone_number(m_raw)
		if not c_mob:
			return None
		f_name = safe_str(obj.get("first_name") or obj.get("First Name") or obj.get("firstname") or "")
		l_name = safe_str(obj.get("last_name") or obj.get("Last Name") or obj.get("lastname") or "")
		m_name = safe_str(obj.get("member_name") or obj.get("Member Name") or obj.get("name") or "")
		if not m_name:
			m_name = f"{f_name} {l_name}".strip() or fallback_name
		e_val = str(obj.get("email") or obj.get("Email") or obj.get("email_id") or fallback_email or "").strip().lower()
		comp = safe_str(obj.get("company_name") or obj.get("Company") or obj.get("company") or fallback_company or "")
		ind = safe_str(obj.get("industry") or obj.get("Industry") or fallback_industry or "")
		chap = safe_str(obj.get("chapter_name") or obj.get("Chapter Name") or obj.get("chapter") or fallback_chapter or "")
		return {
			"mobile_no": c_mob,
			"first_name": f_name,
			"last_name": l_name,
			"member_name": m_name,
			"email": e_val,
			"company_name": comp,
			"industry": ind,
			"chapter_name": chap,
		}

	# Handle case where request payload is directly a list: e.g. [{"mobile_no": ...}, ...]
	if isinstance(data, list):
		raw_items = data
		default_name = ""
		default_email = ""
		default_company = ""
		default_industry = ""
		default_chapter = ""
	else:
		raw_items = (
			data.get("members")
			or data.get("items")
			or data.get("data")
			or kwargs.get("members")
			or kwargs.get("items")
		)
		default_name = safe_str(data.get("member_name") or data.get("name") or kwargs.get("member_name") or "")
		default_email = str(data.get("email") or data.get("Email") or kwargs.get("email") or "").strip().lower()
		default_company = safe_str(data.get("company_name") or data.get("Company") or data.get("company") or "")
		default_industry = safe_str(data.get("industry") or data.get("Industry") or "")
		default_chapter = safe_str(data.get("chapter_name") or data.get("Chapter Name") or data.get("chapter") or "")

	items_to_process = []
	duplicates = []
	errors = []
	seen_mobs = set()
	seen_emails = set()

	raw_list = []
	if raw_items and isinstance(raw_items, (list, tuple)):
		raw_list = list(raw_items)
	else:
		raw_mobs = data.get("mobile_numbers") or data.get("numbers") or kwargs.get("mobile_numbers")
		if raw_mobs and isinstance(raw_mobs, (list, tuple)):
			raw_list = list(raw_mobs)
		else:
			raw_mob = (
				data.get("mobile_no")
				or data.get("Phone Number")
				or data.get("phone_number")
				or data.get("mobile")
				or data.get("phone")
				or kwargs.get("mobile_no")
			)
			if raw_mob:
				for part in str(raw_mob).split(","):
					single_obj = dict(data) if isinstance(data, dict) else {}
					single_obj["mobile_no"] = part.strip()
					raw_list.append(single_obj)
			elif isinstance(data, dict) and (data.get("email") or data.get("Email")):
				raw_list.append(dict(data))

	if not raw_list:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Mobile Number (mobile_no) and Email (email) or members list are required!",
		}

	for idx, it in enumerate(raw_list, start=1):
		parsed = _extract_member_dict(it, default_name, default_email, default_company, default_industry, default_chapter)
		if not parsed or not parsed.get("mobile_no") or len(parsed["mobile_no"]) != 10:
			errors.append({"row": idx, "item": it, "reason": "Valid 10-digit Mobile Number (mobile_no) is mandatory."})
			continue
		em = str(parsed.get("email") or "").strip().lower()
		parsed["email"] = em
		if em:
			if "@" not in em or "." not in em.split("@")[-1]:
				errors.append({"row": idx, "mobile_no": parsed["mobile_no"], "email": em, "reason": "Valid Email Address format is required."})
				continue
			if em in seen_emails:
				duplicates.append({"row": idx, "mobile_no": parsed["mobile_no"], "email": em, "reason": f"Duplicate email '{em}' in request batch."})
				continue
			seen_emails.add(em)
		if parsed["mobile_no"] in seen_mobs:
			duplicates.append({"row": idx, "mobile_no": parsed["mobile_no"], "email": em, "reason": f"Duplicate mobile_no '{parsed['mobile_no']}' in request batch."})
			continue
		seen_mobs.add(parsed["mobile_no"])
		items_to_process.append(parsed)

	created = []
	for item in items_to_process:
		mob = item["mobile_no"]
		em = item["email"]

		# Strict duplicate check against DB (No overwrite allowed!)
		existing_mob = frappe.db.get_value("ConnectFest BNI Member", {"mobile_no": mob}, "name")
		if existing_mob:
			duplicates.append({
				"mobile_no": mob,
				"email": em,
				"reason": f"Mobile number '{mob}' is already registered as a BNI Member.",
			})
			continue

		if em:
			existing_email = frappe.db.sql(
				"SELECT name, mobile_no FROM `tabConnectFest BNI Member` WHERE LOWER(TRIM(email)) = %(em)s LIMIT 1",
				{"em": em},
				as_dict=True,
			)
			if existing_email:
				duplicates.append({
					"mobile_no": mob,
					"email": em,
					"reason": f"Email '{em}' is already registered as a BNI Member (Mobile: {existing_email[0].get('mobile_no')}).",
				})
				continue

		doc = frappe.get_doc({
			"doctype": "ConnectFest BNI Member",
			"mobile_no": mob,
			"first_name": item.get("first_name") or "",
			"last_name": item.get("last_name") or "",
			"member_name": item.get("member_name") or "",
			"email": em,
			"company_name": item.get("company_name") or "",
			"industry": item.get("industry") or "",
			"chapter_name": item.get("chapter_name") or "",
		})
		doc.insert(ignore_permissions=True)
		created.append({
			"name": doc.name,
			"mobile_no": mob,
			"first_name": doc.first_name,
			"last_name": doc.last_name,
			"member_name": doc.member_name,
			"email": doc.email,
			"company_name": doc.company_name,
			"industry": doc.industry,
			"chapter_name": doc.chapter_name,
		})

	if created:
		frappe.db.commit()

	if not created and (duplicates or errors):
		frappe.local.response["http_status_code"] = 409 if (duplicates and not errors) else 400
		first_msg = (duplicates[0]["reason"] if duplicates else errors[0]["reason"])
		return {
			"status": "error",
			"message": first_msg,
			"created": created,
			"duplicates": duplicates,
			"errors": errors,
			"created_count": 0,
			"duplicate_count": len(duplicates),
			"error_count": len(errors),
		}

	return {
		"status": "success",
		"message": f"Created {len(created)} BNI Member(s) (Duplicates skipped: {len(duplicates)}, Errors: {len(errors)})",
		"created": created,
		"duplicates": duplicates,
		"errors": errors,
		"created_count": len(created),
		"duplicate_count": len(duplicates),
		"error_count": len(errors),
		"total_processed": len(raw_list),
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
		fields=["name", "first_name", "last_name", "member_name", "mobile_no", "email", "company_name", "industry", "chapter_name", "creation", "modified"],
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
			m_name = search in (m.member_name or "").lower() or search in (m.first_name or "").lower() or search in (m.last_name or "").lower()
			m_email = search in (m.email or "").lower()
			m_comp = search in (m.company_name or "").lower() or search in (m.chapter_name or "").lower()
			if not (m_mob or m_name or m_email or m_comp):
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
			["name", "first_name", "last_name", "member_name", "mobile_no", "email", "company_name", "industry", "chapter_name", "creation", "modified"],
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
	if "first_name" in data:
		doc.first_name = safe_str(data.get("first_name"))
	if "last_name" in data:
		doc.last_name = safe_str(data.get("last_name"))
	new_name = data.get("member_name") if "member_name" in data else data.get("name") if "name" in data else None
	if new_name is not None:
		doc.member_name = safe_str(new_name)
	elif "first_name" in data or "last_name" in data:
		doc.member_name = f"{doc.first_name or ''} {doc.last_name or ''}".strip()

	if "email" in data:
		new_email = str(data.get("email") or "").strip().lower()
		if new_email:
			if "@" not in new_email or "." not in new_email.split("@")[-1]:
				frappe.local.response["http_status_code"] = 400
				return {"status": "error", "message": "Valid Email Address (email) is required."}
			dup_email = frappe.db.sql(
				"SELECT name FROM `tabConnectFest BNI Member` WHERE LOWER(TRIM(email)) = %(em)s AND name != %(cur)s LIMIT 1",
				{"em": new_email, "cur": doc.name},
				as_dict=True,
			)
			if dup_email:
				frappe.local.response["http_status_code"] = 409
				return {"status": "error", "message": f"Email '{new_email}' is already assigned to another BNI Member"}
			doc.email = new_email
		else:
			doc.email = ""
	if "company_name" in data or "company" in data:
		doc.company_name = safe_str(data.get("company_name") if "company_name" in data else data.get("company"))
	if "industry" in data:
		doc.industry = safe_str(data.get("industry"))
	if "chapter_name" in data or "chapter" in data:
		doc.chapter_name = safe_str(data.get("chapter_name") if "chapter_name" in data else data.get("chapter"))

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


def admin_delete_bni_member(mobile_no=None, mobile_numbers=None, select_all=None, delete_all=None, **kwargs):
	"""
	Admin API: Delete single, bulk, or all BNI members.
	Accepts:
	- select_all / delete_all / all: 1 or True -> Instantly empties the entire BNI directory (optimized SQL)
	- mobile_no: Single string, comma-separated string, or "all" / "*"
	- mobile_numbers: List of mobile numbers (e.g. ["9876543210", "9876543211"]) or "all"
	- numbers / items / ids / members: Aliases for bulk list
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("BNI member deletion")
		if err:
			return err

	data = extract_request_data(kwargs)
	if select_all is not None and "select_all" not in data:
		data["select_all"] = select_all
	if delete_all is not None and "delete_all" not in data:
		data["delete_all"] = delete_all

	# --- Optimized "Select All / Delete All / Empty" Fast-Path ---
	is_delete_all = False
	for k in ("select_all", "delete_all", "all", "empty", "truncate", "clear", "wipe"):
		val = data.get(k)
		if val is not None:
			if isinstance(val, bool) and val:
				is_delete_all = True
				break
			if parse_boolean(val):
				is_delete_all = True
				break
			if str(val).strip().lower() in ("1", "true", "yes", "y", "all", "select_all", "delete_all"):
				is_delete_all = True
				break

	if not is_delete_all:
		action_str = str(data.get("action") or "").strip().lower()
		if action_str in ("all", "delete_all", "select_all", "empty", "truncate", "clear", "wipe"):
			is_delete_all = True

	if not is_delete_all:
		raw_mob_check = str(mobile_no or data.get("mobile_no") or data.get("mobile") or data.get("phone") or "").strip().lower()
		if raw_mob_check in ("all", "*", "select_all", "delete_all", "empty", "truncate"):
			is_delete_all = True

	if not is_delete_all:
		raw_mobs_check = mobile_numbers or data.get("mobile_numbers") or kwargs.get("mobile_numbers")
		if isinstance(raw_mobs_check, str) and raw_mobs_check.strip().lower() in ("all", "*", "select_all", "delete_all", "empty"):
			is_delete_all = True
		elif isinstance(raw_mobs_check, (list, tuple)) and len(raw_mobs_check) == 1:
			first_val = str(raw_mobs_check[0]).strip().lower()
			if first_val in ("all", "*", "select_all", "delete_all", "empty"):
				is_delete_all = True

	if is_delete_all:
		total_count = frappe.db.count("ConnectFest BNI Member")
		if total_count == 0:
			return {
				"status": "success",
				"message": "BNI Member directory is already empty.",
				"deleted_count": 0,
				"all_deleted": True,
				"is_empty": True,
			}

		# Ultra-fast, optimized complete deletion
		frappe.db.sql("DELETE FROM `tabConnectFest BNI Member`")
		frappe.db.sql("UPDATE `tabConnectFest Registration` SET is_bni_member = 0")
		clear_bni_member_cache()
		frappe.clear_cache(doctype="ConnectFest BNI Member")
		frappe.db.commit()

		return {
			"status": "success",
			"message": f"Successfully deleted all {total_count} BNI Member(s). The directory is now completely empty.",
			"deleted_count": total_count,
			"all_deleted": True,
			"is_empty": True,
		}

	# --- Specific Mobile Numbers Deletion ---
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
			"message": "Target mobile_no, mobile_numbers list, or select_all=1 is required for deletion.",
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
	Automatically returns False if BOTH bni_check_mobile and bni_check_email are disabled!
	Defaults to True.
	"""
	return get_bni_verification_settings()["crn_bni_only"]


def get_non_bni_extra_percentage():
	"""
	Returns the extra percentage surcharge configured by Admin for non-BNI members.
	Defaults to 10.0 (or whatever Admin configured).
	"""
	try:
		val = frappe.db.get_single_value("ConnectFest Settings", "non_bni_extra_percentage")
		if val is not None and str(val).strip() != "":
			return max(0.0, flt(val))
	except Exception:
		pass
	return 10.0


def calculate_stall_price_for_user(tier, user_mobile=None, user_email=None, is_bni_member=None, custom_base_price=None):
	"""
	Calculates the exact stall price and tax breakdown based on BNI membership status:
	- If attendee mobile_no exists in 'ConnectFest BNI Member' (or is_bni_member is True):
	  Attendee pays the standard stall price (0% extra surcharge).
	- If attendee is NOT a BNI member:
	  Admin-configured extra percentage (non_bni_extra_percentage) is added to base price.
	  Eg: Silver = Rs 100, Extra = 10% -> Base = Rs 110 (+ GST 18%).
	"""
	from connectfest.services.pricing import get_tier_details
	tier_info = get_tier_details(tier) if tier else {"price": custom_base_price or 0.0, "gst_percent": 18.0}
	original_price = flt(custom_base_price if custom_base_price is not None else tier_info.get("price", 0.0))
	gst_pct = flt(tier_info.get("gst_percent", 18.0))

	# Resolve user mobile
	clean_mob = normalize_phone_number(user_mobile) if user_mobile else ""
	if not clean_mob and user_email:
		clean_email = str(user_email).strip().lower()
		clean_mob = (
			frappe.db.get_value("ConnectFest Registration", {"email": clean_email}, "mobile_no")
			or frappe.db.get_value("User", clean_email, "mobile_no")
			or frappe.db.get_value("User", clean_email, "phone")
			or ""
		)
		clean_mob = normalize_phone_number(clean_mob)

	# Check BNI status
	cfg = get_bni_verification_settings()
	bni_check_active = cfg.get("check_mobile") or cfg.get("check_email")

	is_bni = False
	if not bni_check_active:
		# If Admin disabled both Mobile and Email BNI checks, verification is completely OFF:
		# Everyone gets standard stall pricing with 0% extra surcharge!
		is_bni = True
		surcharge_pct = 0.0
		surcharge_amt = 0.0
		final_base_price = original_price
	else:
		# Strictly verify against ConnectFest BNI Member DocType!
		if clean_mob or user_email:
			is_bni, _ = is_valid_bni_member(mobile_no=clean_mob, email=user_email)
		else:
			is_bni = False

		if is_bni:
			surcharge_pct = 0.0
			surcharge_amt = 0.0
			final_base_price = original_price
		else:
			surcharge_pct = get_non_bni_extra_percentage()
			surcharge_amt = round(original_price * (surcharge_pct / 100.0), 2)
			final_base_price = round(original_price + surcharge_amt, 2)

	tax_amount = round(final_base_price * (gst_pct / 100.0), 2)
	total_amount = round(final_base_price + tax_amount, 2)

	return {
		"tier": tier,
		"is_bni_member": is_bni,
		"mobile_no": clean_mob,
		"original_price": original_price,
		"non_bni_extra_percentage": surcharge_pct,
		"surcharge_amount": surcharge_amt,
		"base_amount": final_base_price,
		"gst_percent": gst_pct,
		"tax_amount": tax_amount,
		"total_amount": total_amount,
	}


def admin_toggle_crn_bni_restriction(enable=None, **kwargs):
	"""
	Admin API: Toggle or set CRN BNI member restriction & dynamic validation rules:
	- enable / crn_bni_only: 1 (ON) / 0 (OFF)
	- bni_check_mobile (or check_mobile): 1 (ON) / 0 (OFF)
	- bni_check_email (or check_email): 1 (ON) / 0 (OFF)
	  * Note: If BOTH bni_check_mobile and bni_check_email are OFF (0), crn_bni_only turns OFF automatically!
	- non_bni_extra_percentage (or extra_percentage, percent): e.g. 10.0
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("CRN BNI restriction controls")
		if err:
			return err

	data = extract_request_data(kwargs)
	cfg = get_bni_verification_settings()

	raw_mob_chk = (
		data.get("bni_check_mobile")
		if "bni_check_mobile" in data
		else data.get("check_mobile")
		if "check_mobile" in data
		else data.get("mobile_check")
		if "mobile_check" in data
		else kwargs.get("bni_check_mobile")
	)
	raw_email_chk = (
		data.get("bni_check_email")
		if "bni_check_email" in data
		else data.get("check_email")
		if "check_email" in data
		else data.get("email_check")
		if "email_check" in data
		else kwargs.get("bni_check_email")
	)

	new_check_mob = (1 if (parse_boolean(raw_mob_chk) or str(raw_mob_chk).strip().lower() in ("1", "true", "yes", "on")) else 0) if raw_mob_chk is not None else (1 if cfg["check_mobile"] else 0)
	new_check_email = (1 if (parse_boolean(raw_email_chk) or str(raw_email_chk).strip().lower() in ("1", "true", "yes", "on")) else 0) if raw_email_chk is not None else (1 if cfg["check_email"] else 0)

	raw_val = enable if enable is not None else data.get("enable") if "enable" in data else data.get("is_enabled") if "is_enabled" in data else data.get("crn_bni_only") if "crn_bni_only" in data else kwargs.get("enable")

	curr = cfg["crn_bni_only"]
	has_val = raw_val is not None
	has_other = any(k in data for k in ("non_bni_extra_percentage", "extra_percentage", "percent", "bni_check_mobile", "check_mobile", "mobile_check", "bni_check_email", "check_email", "email_check"))

	if not has_val and not has_other:
		new_val = 0 if curr else 1
	elif has_val:
		new_val = 1 if (parse_boolean(raw_val) or str(raw_val).strip().lower() in ("1", "true", "yes", "on")) else 0
	else:
		new_val = 1 if curr else 0

	# If user requested to turn ON CRN restriction (new_val == 1) but both checks were OFF in settings,
	# and user did NOT explicitly pass them as OFF in this request, automatically enable both checks!
	if new_val == 1 and new_check_mob == 0 and new_check_email == 0:
		if raw_mob_chk is None and raw_email_chk is None:
			new_check_mob = 1
			new_check_email = 1
		else:
			new_val = 0
	elif new_check_mob == 0 and new_check_email == 0:
		new_val = 0

	frappe.db.set_single_value("ConnectFest Settings", "crn_bni_only", new_val)
	frappe.db.set_single_value("ConnectFest Settings", "bni_check_mobile", new_check_mob)
	frappe.db.set_single_value("ConnectFest Settings", "bni_check_email", new_check_email)

	# Update non-BNI extra percentage if provided
	raw_pct = (
		data.get("non_bni_extra_percentage")
		if "non_bni_extra_percentage" in data
		else data.get("extra_percentage")
		if "extra_percentage" in data
		else data.get("extra_percent")
		if "extra_percent" in data
		else data.get("percent")
		if "percent" in data
		else kwargs.get("non_bni_extra_percentage")
		if "non_bni_extra_percentage" in kwargs
		else kwargs.get("extra_percentage")
	)

	curr_pct = get_non_bni_extra_percentage()
	if raw_pct is not None:
		try:
			new_pct = max(0.0, flt(raw_pct))
			frappe.db.set_single_value("ConnectFest Settings", "non_bni_extra_percentage", new_pct)
			curr_pct = new_pct
		except Exception:
			pass

	frappe.db.commit()
	try:
		frappe.clear_cache(doctype="ConnectFest Settings")
	except Exception:
		pass

	try:
		from connectfest.services.stalls import invalidate_stalls_cache
		from connectfest.services.pricing import invalidate_pricing_cache
		invalidate_stalls_cache()
		invalidate_pricing_cache()
	except Exception:
		pass

	return {
		"status": "success",
		"message": f"CRN BNI restriction {'enabled' if new_val else 'disabled'} (Mobile Check: {'ON' if new_check_mob else 'OFF'}, Email Check: {'ON' if new_check_email else 'OFF'}). Non-BNI extra percentage set to {curr_pct}%.",
		"crn_bni_only": new_val,
		"is_enabled": bool(new_val),
		"bni_check_mobile": new_check_mob,
		"bni_check_email": new_check_email,
		"non_bni_extra_percentage": curr_pct,
	}


def admin_set_non_bni_extra_percentage(percentage=None, **kwargs):
	"""
	Admin API: Dedicated endpoint to set the non-BNI member extra surcharge percentage.
	"""
	if not (kwargs.get("skip_auth") or kwargs.get("is_internal") or getattr(frappe.flags, "in_test", False)):
		from connectfest.services.auth import check_admin_permission
		err = check_admin_permission("Non-BNI extra pricing settings")
		if err:
			return err

	data = extract_request_data(kwargs)
	raw_pct = percentage if percentage is not None else data.get("percentage") or data.get("percent") or data.get("non_bni_extra_percentage")

	if raw_pct is None:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Percentage (percentage) is required."}

	new_pct = max(0.0, flt(raw_pct))
	frappe.db.set_single_value("ConnectFest Settings", "non_bni_extra_percentage", new_pct)
	frappe.db.commit()

	try:
		from connectfest.services.pricing import invalidate_pricing_cache
		invalidate_pricing_cache()
	except Exception:
		pass

	return {
		"status": "success",
		"message": f"Non-BNI member extra percentage updated to {new_pct}%.",
		"non_bni_extra_percentage": new_pct,
	}


def get_non_bni_extra_percentage_info(**kwargs):
	"""
	Public API: Returns the current CRN restriction status, mobile/email check flags, and non-BNI extra percentage.
	"""
	cfg = get_bni_verification_settings()
	return {
		"status": "success",
		"crn_bni_only": 1 if cfg["crn_bni_only"] else 0,
		"is_crn_bni_restriction_enabled": cfg["crn_bni_only"],
		"bni_check_mobile": 1 if cfg["check_mobile"] else 0,
		"bni_check_email": 1 if cfg["check_email"] else 0,
		"non_bni_extra_percentage": get_non_bni_extra_percentage(),
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
