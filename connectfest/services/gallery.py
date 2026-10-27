import json
import uuid
import frappe
from frappe.utils import cint, now_datetime
from connectfest.services.common import (
	extract_request_data,
	parse_boolean,
	handle_api_errors,
	save_image_from_base64_or_url,
)
from connectfest.services.auth import check_admin_permission


def _extract_id_list(raw_val):
	"""Extracts a list of clean string IDs from a list, JSON string, or comma-separated string."""
	if not raw_val:
		return []
	if isinstance(raw_val, list):
		return [str(item).strip() for item in raw_val if str(item).strip()]
	if isinstance(raw_val, str):
		val = raw_val.strip()
		if val.startswith("[") and val.endswith("]"):
			try:
				parsed = json.loads(val)
				if isinstance(parsed, list):
					return [str(item).strip() for item in parsed if str(item).strip()]
			except Exception:
				pass
			val = val.strip("[]")
		return [s.strip() for s in val.split(",") if s.strip()]
	return [str(raw_val).strip()]


def _parse_images_payload(raw_images, file_prefix="gallery"):
	"""
	Normalizes multiple images input into a standardized list of dicts:
	[{"image": "/files/...", "caption": "...", "display_order": 0}]
	Accepts:
	- List of strings (URLs, local paths, or base64 data URLs)
	- List of dicts (with 'image', 'caption', 'display_order')
	- JSON string of list
	- Comma-separated string of image URLs
	"""
	if not raw_images:
		return []

	parsed = raw_images
	if isinstance(raw_images, str):
		raw_str = raw_images.strip()
		if raw_str.startswith("[") and raw_str.endswith("]"):
			try:
				parsed = json.loads(raw_str)
			except Exception:
				parsed = [s.strip() for s in raw_str.strip("[]").split(",") if s.strip()]
		elif "," in raw_str:
			parsed = [s.strip() for s in raw_str.split(",") if s.strip()]
		else:
			parsed = [raw_str]

	if not isinstance(parsed, list):
		parsed = [parsed]

	result = []
	for idx, item in enumerate(parsed):
		img_url = ""
		caption = ""
		order = idx

		if isinstance(item, str):
			img_url = save_image_from_base64_or_url(item, file_name_prefix=f"{file_prefix}_{idx+1}")
		elif isinstance(item, dict):
			raw_url = item.get("image") or item.get("image_url") or item.get("url") or ""
			caption = (item.get("caption") or item.get("title") or "").strip()
			order = cint(item.get("display_order", idx))
			img_url = save_image_from_base64_or_url(raw_url, file_name_prefix=f"{file_prefix}_{idx+1}")

		if img_url:
			result.append({
				"image": img_url,
				"caption": caption,
				"display_order": order,
			})

	return result


GALLERY_CACHE_KEY = "connectfest:gallery:published_default"


def invalidate_gallery_cache():
	"""Invalidates Redis cache keys for gallery."""
	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(GALLERY_CACHE_KEY))
	except Exception:
		pass


def _format_gallery_doc(doc_or_dict, include_images=True, images_by_parent=None):
	"""Standardizes gallery item output dictionary for frontend JSON consumption."""
	if isinstance(doc_or_dict, dict):
		name = doc_or_dict.get("name")
		title = doc_or_dict.get("title")
		category = doc_or_dict.get("category") or "General"
		description = doc_or_dict.get("description") or ""
		cover_image = doc_or_dict.get("cover_image") or ""
		is_pinned = cint(doc_or_dict.get("is_pinned", 0))
		is_published = cint(doc_or_dict.get("is_published", 1))
		display_order = cint(doc_or_dict.get("display_order", 0))
		creation = str(doc_or_dict.get("creation") or "")
		modified = str(doc_or_dict.get("modified") or "")
	else:
		name = doc_or_dict.name
		title = doc_or_dict.title
		category = doc_or_dict.category or "General"
		description = doc_or_dict.description or ""
		cover_image = doc_or_dict.cover_image or ""
		is_pinned = cint(doc_or_dict.is_pinned)
		is_published = cint(doc_or_dict.is_published)
		display_order = cint(doc_or_dict.display_order)
		creation = str(doc_or_dict.creation)
		modified = str(doc_or_dict.modified)

	images_list = []
	if include_images:
		if images_by_parent is not None:
			images_list = images_by_parent.get(name, [])
		elif hasattr(doc_or_dict, "images") and doc_or_dict.images:
			for row in doc_or_dict.images:
				images_list.append({
					"image": row.image,
					"caption": row.caption or "",
					"display_order": cint(row.display_order),
				})
		elif isinstance(doc_or_dict, dict) and "images" in doc_or_dict and isinstance(doc_or_dict["images"], list):
			images_list = doc_or_dict["images"]
		else:
			# Query child table from database
			child_rows = frappe.db.get_all(
				"ConnectFest Gallery Item Image",
				filters={"parent": name, "parenttype": "ConnectFest Gallery"},
				fields=["image", "caption", "display_order"],
				order_by="display_order ASC, idx ASC"
			)
			images_list = [
				{
					"image": r.image,
					"caption": r.caption or "",
					"display_order": cint(r.display_order),
				}
				for r in child_rows
			]

	if not cover_image and images_list:
		cover_image = images_list[0]["image"]

	return {
		"id": name,
		"name": name,
		"title": title,
		"category": category,
		"description": description,
		"cover_image": cover_image,
		"is_pinned": is_pinned,
		"is_pinned_bool": bool(is_pinned),
		"is_published": is_published,
		"display_order": display_order,
		"image_count": len(images_list),
		"images": images_list,
		"created_at": creation,
		"updated_at": modified,
	}


# ==============================================================================
# 1. PUBLIC WEBSITE GALLERY APIS (For Visitors / Attendees)
# ==============================================================================

@handle_api_errors("ConnectFest Public Gallery Error")
def get_gallery(**kwargs):
	"""
	Public Gallery API for User Website.
	- Whitelisted for Guests (Attendees/Visitors).
	- Returns only published gallery items.
	- STRICT SORTING: Pinned items (is_pinned = 1) come FIRST!
	- Supports optional filtering by category, search term, and pagination.
	- Redis cached for sub-millisecond public response time.
	"""
	data = extract_request_data(kwargs)
	category = (data.get("category") or "").strip()
	search = (data.get("search") or data.get("query") or "").strip().lower()
	pinned_only = data.get("is_pinned") or data.get("pinned")

	raw_page = data.get("page")
	raw_limit = data.get("limit") or data.get("page_size") or 50
	page = max(1, cint(raw_page)) if raw_page else 1
	limit = min(100, max(1, cint(raw_limit))) if raw_limit else 50
	offset = (page - 1) * limit

	is_default_query = not category and not search and pinned_only is None and page == 1 and limit == 50
	cache = frappe.cache()
	cache_key = cache.make_key(GALLERY_CACHE_KEY)

	if is_default_query:
		try:
			cached = cache.get(cache_key)
			if cached:
				if isinstance(cached, bytes):
					cached = cached.decode("utf-8")
				return json.loads(cached)
		except Exception:
			pass

	# Base query conditions
	conditions = ["is_published = 1"]
	params = {}

	if category and category.lower() != "all":
		conditions.append("LOWER(category) = %(cat)s")
		params["cat"] = category.lower()

	if pinned_only is not None and str(pinned_only).strip() != "":
		p_val = parse_boolean(pinned_only)
		conditions.append("is_pinned = %(pinned)s")
		params["pinned"] = p_val

	if search:
		conditions.append("(LOWER(title) LIKE %(q)s OR LOWER(description) LIKE %(q)s OR LOWER(category) LIKE %(q)s)")
		params["q"] = f"%{search}%"

	where_clause = " AND ".join(conditions)

	# Total count
	total_count = frappe.db.sql(f"""
		SELECT COUNT(*) FROM `tabConnectFest Gallery`
		WHERE {where_clause}
	""", params)[0][0]

	# Pinned count
	pinned_count = frappe.db.sql("""
		SELECT COUNT(*) FROM `tabConnectFest Gallery`
		WHERE is_published = 1 AND is_pinned = 1
	""")[0][0]

	# Fetch gallery items ordered strictly by is_pinned DESC, display_order ASC, creation DESC
	items = frappe.db.sql(f"""
		SELECT name, title, category, description, cover_image, is_pinned, is_published,
		       display_order, creation, modified
		FROM `tabConnectFest Gallery`
		WHERE {where_clause}
		ORDER BY is_pinned DESC, display_order ASC, creation DESC
		LIMIT %(limit)s OFFSET %(offset)s
	""", {**params, "limit": limit, "offset": offset}, as_dict=True)

	# Batch fetch images in a single SQL query (Fixes N+1 query problem)
	images_by_parent = {}
	item_names = [it["name"] for it in items if it.get("name")]
	if item_names:
		placeholders = ", ".join(["%s"] * len(item_names))
		child_images = frappe.db.sql(f"""
			SELECT parent, image, caption, display_order
			FROM `tabConnectFest Gallery Item Image`
			WHERE parent IN ({placeholders}) AND parenttype = 'ConnectFest Gallery'
			ORDER BY display_order ASC, idx ASC
		""", tuple(item_names), as_dict=True)
		for img in child_images:
			images_by_parent.setdefault(img["parent"], []).append({
				"image": img["image"],
				"caption": img.get("caption") or "",
				"display_order": cint(img.get("display_order")),
			})

	formatted_items = [_format_gallery_doc(it, include_images=True, images_by_parent=images_by_parent) for it in items]

	# Distinct published categories for tab filters on website
	db_cats = frappe.db.sql("""
		SELECT DISTINCT category FROM `tabConnectFest Gallery`
		WHERE is_published = 1 AND category IS NOT NULL AND category != ''
		ORDER BY category ASC
	""", as_dict=True)
	available_categories = ["All"] + [c.category for c in db_cats]

	res = {
		"status": "success",
		"total_items": total_count,
		"pinned_count": pinned_count,
		"pagination": {
			"current_page": page,
			"page_size": limit,
			"total_items": total_count,
			"total_pages": (total_count + limit - 1) // limit if limit else 1,
			"has_next": (offset + limit) < total_count,
			"has_prev": page > 1,
		},
		"categories": available_categories,
		"active_category": category or "All",
		"gallery": formatted_items,
	}

	if is_default_query:
		try:
			cache.setex(cache_key, 300, json.dumps(res, default=str))
		except Exception:
			pass

	return res


@handle_api_errors("ConnectFest Public Gallery Item Error")
def get_gallery_item(gallery_id=None, name=None, **kwargs):
	"""
	Public API to view a single gallery item and its images.
	"""
	data = extract_request_data(kwargs)
	docname = (gallery_id or name or data.get("gallery_id") or data.get("name") or data.get("id") or "").strip()

	if not docname:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "gallery_id is required"}

	if not frappe.db.exists("ConnectFest Gallery", docname):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Gallery item '{docname}' not found"}

	doc = frappe.get_doc("ConnectFest Gallery", docname)
	if not doc.is_published:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Gallery item '{docname}' is not published"}

	return {
		"status": "success",
		"gallery_item": _format_gallery_doc(doc, include_images=True),
	}


# ==============================================================================
# 2. ADMIN CRUD & PINNING APIS (For Event Organizers & Admins)
# ==============================================================================

@handle_api_errors("ConnectFest Admin Create Gallery Error")
def admin_create_gallery_item(title=None, images=None, description=None, category=None, is_pinned=None, pinned=None, cover_image=None, display_order=None, is_published=None, **kwargs):
	"""
	Admin API to create a new gallery item with multiple images and optional pinning.
	"""
	err = check_admin_permission("gallery management")
	if err:
		return err

	data = extract_request_data(kwargs)
	title_val = (title or data.get("title") or "").strip()
	desc_val = (description or data.get("description") or "").strip()
	cat_val = (category or data.get("category") or "Highlights").strip()

	raw_pinned = (
		data.get("is_pinned")
		if "is_pinned" in data
		else (data.get("pinned") if "pinned" in data else (is_pinned if is_pinned is not None else (pinned if pinned is not None else 0)))
	)
	pinned_val = parse_boolean(raw_pinned)

	raw_order = (
		data.get("display_order")
		if "display_order" in data
		else (data.get("order") if "order" in data else (display_order if display_order is not None else 0))
	)
	order_val = cint(raw_order)

	raw_pub = (
		data.get("is_published")
		if "is_published" in data
		else (data.get("published") if "published" in data else (is_published if is_published is not None else 1))
	)
	pub_val = parse_boolean(raw_pub)

	raw_imgs = images if images is not None else data.get("images")
	if not raw_imgs:
		single_img = data.get("image") or data.get("image_url") or data.get("file")
		if single_img:
			raw_imgs = [single_img]

	if not title_val:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Title is required for the gallery item"}

	processed_images = _parse_images_payload(raw_imgs, file_prefix="gallery")
	if not processed_images:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "At least one valid image is required for the gallery item"}

	# Determine cover image: explicit cover_image, or fallback to 1st image
	cover_val = (cover_image or data.get("cover_image") or "").strip()
	if not cover_val and processed_images:
		cover_val = processed_images[0]["image"]

	doc = frappe.get_doc({
		"doctype": "ConnectFest Gallery",
		"title": title_val,
		"category": cat_val,
		"description": desc_val,
		"cover_image": cover_val,
		"is_pinned": pinned_val,
		"is_published": pub_val,
		"display_order": order_val,
		"images": [
			{
				"doctype": "ConnectFest Gallery Item Image",
				"image": img["image"],
				"caption": img.get("caption", ""),
				"display_order": img.get("display_order", idx),
			}
			for idx, img in enumerate(processed_images)
		]
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()
	invalidate_gallery_cache()

	return {
		"status": "success",
		"message": f"Gallery item '{doc.title}' created successfully" + (" (📌 Pinned to top!)" if doc.is_pinned else ""),
		"gallery_item": _format_gallery_doc(doc, include_images=True),
	}


@handle_api_errors("ConnectFest Admin Get Gallery Error")
def admin_get_gallery_items(**kwargs):
	"""
	Admin API to view all gallery items (both published and unpublished), sorted by pin status.
	"""
	err = check_admin_permission("gallery management")
	if err:
		return err

	data = extract_request_data(kwargs)
	category = (data.get("category") or "").strip()
	search = (data.get("search") or data.get("query") or "").strip().lower()
	pinned_only = data.get("is_pinned") or data.get("pinned")
	pub_filter = data.get("is_published")

	raw_page = data.get("page")
	raw_limit = data.get("limit") or data.get("page_size") or 50
	page = max(1, cint(raw_page)) if raw_page else 1
	limit = min(100, max(1, cint(raw_limit))) if raw_limit else 50
	offset = (page - 1) * limit

	conditions = ["1=1"]
	params = {}

	if category and category.lower() != "all":
		conditions.append("LOWER(category) = %(cat)s")
		params["cat"] = category.lower()

	if pinned_only is not None and str(pinned_only).strip() != "":
		conditions.append("is_pinned = %(pinned)s")
		params["pinned"] = parse_boolean(pinned_only)

	if pub_filter is not None and str(pub_filter).strip() != "":
		conditions.append("is_published = %(pub)s")
		params["pub"] = parse_boolean(pub_filter)

	if search:
		conditions.append("(LOWER(title) LIKE %(q)s OR LOWER(description) LIKE %(q)s OR LOWER(category) LIKE %(q)s)")
		params["q"] = f"%{search}%"

	where_clause = " AND ".join(conditions)

	total_count = frappe.db.sql(f"SELECT COUNT(*) FROM `tabConnectFest Gallery` WHERE {where_clause}", params)[0][0]
	pinned_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Gallery` WHERE is_pinned = 1")[0][0]
	published_count = frappe.db.sql("SELECT COUNT(*) FROM `tabConnectFest Gallery` WHERE is_published = 1")[0][0]

	items = frappe.db.sql(f"""
		SELECT name, title, category, description, cover_image, is_pinned, is_published,
		       display_order, creation, modified
		FROM `tabConnectFest Gallery`
		WHERE {where_clause}
		ORDER BY is_pinned DESC, display_order ASC, creation DESC
		LIMIT %(limit)s OFFSET %(offset)s
	""", {**params, "limit": limit, "offset": offset}, as_dict=True)

	formatted_items = [_format_gallery_doc(it, include_images=True) for it in items]

	return {
		"status": "success",
		"total_items": total_count,
		"pinned_count": pinned_count,
		"published_count": published_count,
		"pagination": {
			"current_page": page,
			"page_size": limit,
			"total_items": total_count,
			"total_pages": (total_count + limit - 1) // limit if limit else 1,
			"has_next": (offset + limit) < total_count,
			"has_prev": page > 1,
		},
		"gallery": formatted_items,
	}


@handle_api_errors("ConnectFest Admin Update Gallery Error")
def admin_update_gallery_item(gallery_id=None, name=None, title=None, description=None, category=None, is_pinned=None, is_published=None, images=None, cover_image=None, display_order=None, **kwargs):
	"""
	Admin API to update an existing gallery item.
	Allows modifying title, pin status, publishing, and replacing or adding images.
	"""
	err = check_admin_permission("gallery management")
	if err:
		return err

	data = extract_request_data(kwargs)
	docname = (gallery_id or name or data.get("gallery_id") or data.get("name") or data.get("id") or "").strip()

	if not docname:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "gallery_id is required"}

	if not frappe.db.exists("ConnectFest Gallery", docname):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Gallery item '{docname}' not found"}

	doc = frappe.get_doc("ConnectFest Gallery", docname)

	# Update fields if provided
	if title is not None or "title" in data:
		doc.title = (title or data.get("title") or "").strip()
	if description is not None or "description" in data:
		doc.description = (description or data.get("description") or "").strip()
	if category is not None or "category" in data:
		doc.category = (category or data.get("category") or "").strip()
	if is_pinned is not None or "is_pinned" in data or "pinned" in data:
		raw_pin = is_pinned if is_pinned is not None else (data.get("is_pinned") if "is_pinned" in data else data.get("pinned"))
		doc.is_pinned = parse_boolean(raw_pin)
	if is_published is not None or "is_published" in data or "published" in data:
		raw_pub = is_published if is_published is not None else (data.get("is_published") if "is_published" in data else data.get("published"))
		doc.is_published = parse_boolean(raw_pub)
	if display_order is not None or "display_order" in data or "order" in data:
		raw_ord = display_order if display_order is not None else (data.get("display_order") if "display_order" in data else data.get("order"))
		doc.display_order = cint(raw_ord)

	# Update images if provided
	raw_imgs = images if images is not None else data.get("images")
	append_images = parse_boolean(data.get("append_images", 0))

	if raw_imgs is not None:
		new_images = _parse_images_payload(raw_imgs, file_prefix=f"gallery_{docname}")
		if new_images:
			if append_images:
				start_idx = len(doc.images)
				for idx, img in enumerate(new_images):
					doc.append("images", {
						"image": img["image"],
						"caption": img.get("caption", ""),
						"display_order": img.get("display_order", start_idx + idx),
					})
			else:
				# Replace all images
				doc.images = []
				for idx, img in enumerate(new_images):
					doc.append("images", {
						"image": img["image"],
						"caption": img.get("caption", ""),
						"display_order": img.get("display_order", idx),
					})

	# Cover image update
	if cover_image is not None or "cover_image" in data:
		raw_cover = (cover_image or data.get("cover_image") or "").strip()
		if raw_cover:
			doc.cover_image = save_image_from_base64_or_url(raw_cover, file_name_prefix=f"gallery_cover_{docname}")
	elif not doc.cover_image and doc.images:
		doc.cover_image = doc.images[0].image

	doc.flags.ignore_permissions = True
	try:
		doc.save()
	except Exception as e:
		if "1205" in str(e) or "Lock wait timeout" in str(e):
			frappe.db.rollback()
			import time
			time.sleep(0.5)
			doc.save()
		else:
			raise
	frappe.db.commit()
	invalidate_gallery_cache()

	return {
		"status": "success",
		"message": f"Gallery item '{doc.title}' updated successfully",
		"gallery_item": _format_gallery_doc(doc, include_images=True),
	}


@handle_api_errors("ConnectFest Admin Delete Gallery Error")
def admin_delete_gallery_item(gallery_id=None, name=None, gallery_ids=None, ids=None, **kwargs):
	"""
	Admin API to delete one or multiple gallery items.
	Supports:
	- Single gallery_id: e.g. "GAL-00001"
	- Multiple gallery_ids: list or comma-separated string e.g. ["GAL-00001", "GAL-00002"] or "GAL-00001,GAL-00002"
	"""
	err = check_admin_permission("gallery management")
	if err:
		return err

	data = extract_request_data(kwargs)
	raw_ids = gallery_ids or ids or data.get("gallery_ids") or data.get("ids") or data.get("items")
	single_id = (gallery_id or name or data.get("gallery_id") or data.get("name") or data.get("id") or "").strip()

	target_ids = []
	if raw_ids:
		target_ids.extend(_extract_id_list(raw_ids))
	if single_id:
		target_ids.extend(_extract_id_list(single_id))

	# Deduplicate while preserving order
	seen = set()
	clean_ids = []
	for gid in target_ids:
		if gid and gid not in seen:
			seen.add(gid)
			clean_ids.append(gid)

	if not clean_ids:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "gallery_id or gallery_ids is required"}

	deleted_ids = []
	not_found_ids = []
	deleted_titles = []

	for gid in clean_ids:
		if frappe.db.exists("ConnectFest Gallery", gid):
			title = frappe.db.get_value("ConnectFest Gallery", gid, "title") or gid
			try:
				frappe.delete_doc("ConnectFest Gallery", gid, ignore_permissions=True, force=True)
				deleted_ids.append(gid)
				deleted_titles.append(title)
			except Exception as ex:
				not_found_ids.append({"id": gid, "reason": str(ex)})
		else:
			not_found_ids.append({"id": gid, "reason": "Not found"})

	frappe.db.commit()
	invalidate_gallery_cache()

	if not deleted_ids:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": "None of the specified gallery items were found",
			"failed_ids": not_found_ids,
		}

	# Format response
	msg = (
		f"Gallery item '{deleted_titles[0]}' ({deleted_ids[0]}) has been deleted successfully"
		if len(deleted_ids) == 1
		else f"Successfully deleted {len(deleted_ids)} gallery item(s)"
	)

	return {
		"status": "success",
		"message": msg,
		"deleted_count": len(deleted_ids),
		"deleted_id": deleted_ids[0] if len(deleted_ids) == 1 else None,
		"deleted_ids": deleted_ids,
		"failed_ids": not_found_ids if not_found_ids else [],
	}


# Alias for bulk delete
admin_delete_gallery_items = admin_delete_gallery_item


@handle_api_errors("ConnectFest Admin Toggle Pin Error")
def admin_toggle_gallery_pin(gallery_id=None, name=None, gallery_ids=None, ids=None, is_pinned=None, **kwargs):
	"""
	Admin API to pin or unpin one or multiple gallery items.
	Supports:
	- Single gallery_id: e.g. "GAL-00001"
	- Multiple gallery_ids: list or comma-separated string e.g. ["GAL-00001", "GAL-00002"] or "GAL-00001,GAL-00002"
	- is_pinned: 1 to pin, 0 to unpin. If omitted:
	  * Single item: toggles (0 -> 1, 1 -> 0)
	  * Multiple items: pins all (1)
	Pinned items always appear first at the top of the website gallery!
	"""
	err = check_admin_permission("gallery management")
	if err:
		return err

	data = extract_request_data(kwargs)
	raw_ids = gallery_ids or ids or data.get("gallery_ids") or data.get("ids") or data.get("items")
	single_id = (gallery_id or name or data.get("gallery_id") or data.get("name") or data.get("id") or "").strip()

	target_ids = []
	if raw_ids:
		target_ids.extend(_extract_id_list(raw_ids))
	if single_id:
		target_ids.extend(_extract_id_list(single_id))

	seen = set()
	clean_ids = []
	for gid in target_ids:
		if gid and gid not in seen:
			seen.add(gid)
			clean_ids.append(gid)

	if not clean_ids:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "gallery_id or gallery_ids is required"}

	pin_input = is_pinned if is_pinned is not None else data.get("is_pinned")

	pinned_ids = []
	unpinned_ids = []
	not_found_ids = []

	for gid in clean_ids:
		if not frappe.db.exists("ConnectFest Gallery", gid):
			not_found_ids.append(gid)
			continue

		current_pinned = cint(frappe.db.get_value("ConnectFest Gallery", gid, "is_pinned"))
		if pin_input is not None and str(pin_input).strip() != "":
			target_pin = 1 if parse_boolean(pin_input) else 0
		else:
			if len(clean_ids) == 1:
				target_pin = 0 if current_pinned else 1
			else:
				target_pin = 1

		frappe.db.set_value("ConnectFest Gallery", gid, "is_pinned", target_pin, update_modified=True)
		if target_pin == 1:
			pinned_ids.append(gid)
		else:
			unpinned_ids.append(gid)

	frappe.db.commit()
	invalidate_gallery_cache()

	total_updated = len(pinned_ids) + len(unpinned_ids)
	if total_updated == 0:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": "None of the specified gallery items were found",
			"failed_ids": not_found_ids,
		}

	# Backward compatibility for single item
	if len(clean_ids) == 1 and total_updated == 1:
		single_target = clean_ids[0]
		is_p = 1 if single_target in pinned_ids else 0
		title = frappe.db.get_value("ConnectFest Gallery", single_target, "title") or single_target
		status_text = "pinned to the top of the website gallery" if is_p else "unpinned"
		return {
			"status": "success",
			"message": f"Gallery item '{title}' has been {status_text}!",
			"gallery_id": single_target,
			"is_pinned": is_p,
			"is_pinned_bool": bool(is_p),
			"pinned_ids": pinned_ids,
			"unpinned_ids": unpinned_ids,
		}

	return {
		"status": "success",
		"message": f"Updated pin status for {total_updated} gallery item(s): {len(pinned_ids)} pinned, {len(unpinned_ids)} unpinned.",
		"total_updated": total_updated,
		"pinned_count": len(pinned_ids),
		"unpinned_count": len(unpinned_ids),
		"pinned_ids": pinned_ids,
		"unpinned_ids": unpinned_ids,
		"failed_ids": not_found_ids,
	}


# Aliases for bulk pin
admin_pin_gallery_items = admin_toggle_gallery_pin
admin_bulk_pin_gallery_items = admin_toggle_gallery_pin


@handle_api_errors("ConnectFest Admin Upload Gallery Image Error")
def admin_upload_gallery_image(file=None, filedata=None, filename=None, is_private=0, **kwargs):
	"""
	Admin Helper API to upload an image directly.
	Supports:
	1. Multipart form-data file upload (Hoppscotch/Postman `file`)
	2. Base64 data URL
	3. Raw binary file content
	Returns public /files/... URL that can be directly used in gallery images.
	"""
	err = check_admin_permission("gallery management")
	if err:
		return err

	data = extract_request_data(kwargs)
	fname = (filename or data.get("filename") or "").strip()

	# Check multipart/form-data files
	uploaded_file = None
	if getattr(frappe, "request", None) and getattr(frappe.request, "files", None):
		uploaded_file = frappe.request.files.get("file") or frappe.request.files.get("image") or frappe.request.files.get("filedata")

	saved_url = ""
	if uploaded_file:
		from frappe.utils.file_manager import save_file
		file_content = uploaded_file.read()
		actual_fname = uploaded_file.filename or fname or f"gallery_{uuid.uuid4().hex[:8]}.jpg"
		saved = save_file(actual_fname, file_content, dt=None, dn=None, is_private=0)
		saved_url = saved.file_url
		fname = actual_fname
	else:
		raw_content = file or filedata or data.get("file") or data.get("filedata") or data.get("image")
		if not raw_content:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "No file or image data provided in request. Use 'file' in multipart/form-data or send base64 image data."}

		saved_url = save_image_from_base64_or_url(raw_content, file_name_prefix="gallery")
		if not fname:
			fname = f"gallery_{uuid.uuid4().hex[:8]}.jpg"

	if not saved_url:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Failed to upload or decode image. Ensure valid image file/data."}

	full_url = frappe.utils.get_url(saved_url) if saved_url.startswith("/") else saved_url
	return {
		"status": "success",
		"message": "Image uploaded successfully",
		"file_url": saved_url,
		"full_url": full_url,
		"filename": fname,
	}
