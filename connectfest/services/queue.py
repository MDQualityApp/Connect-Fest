import json
import frappe
from connectfest.services.common import (
	STALL_QUEUE_KEY,
	VISITOR_QUEUE_KEY,
	STALL_CACHE_PREFIX,
	BATCH_CHUNK_SIZE,
	get_cached_record,
)
from connectfest.services.pricing import STALL_PRICING


def trigger_async_flush():
	"""Trigger non-blocking background queue drain."""
	try:
		frappe.enqueue(
			"connectfest.services.queue.flush_registration_queue",
			queue="short",
			now=False,
			enqueue_after_commit=False,
		)
	except Exception:
		pass


def flush_registration_queue(batch_size=BATCH_CHUNK_SIZE):
	"""
	Worker task: Drains items from Redis lists and bulk inserts into MariaDB.
	"""
	cache = frappe.cache()
	total_stalls = _flush_single_queue(
		cache,
		STALL_QUEUE_KEY,
		"ConnectFest Registration",
		[
			"name",
			"registration_id",
			"full_name",
			"company_name",
			"location",
			"bill",
			"gst",
			"mobile_no",
			"email",
			"is_bni_member",
			"region_name",
			"chapter_name",
			"industry",
			"segment",
			"source_of_info",
			"stall_number",
			"stall_category",
			"amount",
			"payment_status",
			"razorpay_order_id",
			"razorpay_payment_id",
			"razorpay_signature",
			"payment_date",
			"status",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"docstatus",
			"idx",
		],
		batch_size,
	)

	total_visitors = _flush_single_queue(
		cache,
		VISITOR_QUEUE_KEY,
		"ConnectFest Visitor Registration",
		[
			"name",
			"registration_id",
			"full_name",
			"email",
			"mobile_no",
			"which_day",
			"pass_code",
			"qr_payload",
			"status",
			"source_of_info",
			"area_of_interest",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"docstatus",
			"idx",
		],
		batch_size,
	)

	return total_stalls, total_visitors


def _flush_single_queue(cache, queue_key_name, doctype, db_fields, batch_size):
	raw_key = cache.make_key(queue_key_name)
	processed = 0

	while True:
		queue_len = cache.llen(queue_key_name)
		if not queue_len:
			break

		fetch_count = min(queue_len, batch_size)
		pipe = cache.pipeline()
		for _ in range(fetch_count):
			pipe.lpop(raw_key)
		raw_items = pipe.execute()

		rows_to_insert = []
		for raw in raw_items:
			if not raw:
				continue
			try:
				if isinstance(raw, bytes):
					raw = raw.decode("utf-8")
				item = json.loads(raw)
				reg_id = item.get("registration_id")
				if reg_id and doctype == "ConnectFest Registration":
					cached_item = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")
					if cached_item:
						item.update(cached_item)
				row = tuple(item.get(f) for f in db_fields)
				rows_to_insert.append(row)
			except Exception as parse_err:
				frappe.log_error(f"{doctype} Queue Parse Error", str(parse_err))

		if rows_to_insert:
			try:
				frappe.db.bulk_insert(
					doctype,
					fields=db_fields,
					values=rows_to_insert,
					ignore_duplicates=True,
				)
				frappe.db.commit()
				processed += len(rows_to_insert)
			except Exception as insert_err:
				frappe.log_error(f"{doctype} Bulk Insert Error", str(insert_err))
				frappe.db.rollback()
				restore_pipe = cache.pipeline()
				for r in reversed(raw_items):
					if r:
						restore_pipe.lpush(raw_key, r)
				restore_pipe.execute()
				break

		if fetch_count < batch_size:
			break

	return processed


def get_queue_stats():
	"""Monitor queue lengths and total database counts for both Stalls and Visitors."""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("queue statistics")
	if err:
		return err

	cache = frappe.cache()
	try:
		pending_stalls = cache.llen(STALL_QUEUE_KEY)
	except Exception:
		pending_stalls = 0

	try:
		pending_visitors = cache.llen(VISITOR_QUEUE_KEY)
	except Exception:
		pending_visitors = 0

	total_stalls_db = frappe.db.count("ConnectFest Registration")
	total_visitors_db = frappe.db.count("ConnectFest Visitor Registration")

	return {
		"status": "success",
		"pending_stall_queue": pending_stalls,
		"pending_visitor_queue": pending_visitors,
		"total_stalls_in_db": total_stalls_db,
		"total_visitors_in_db": total_visitors_db,
		"batch_chunk_size": BATCH_CHUNK_SIZE,
		"stall_pricing": STALL_PRICING,
	}


def flush_queue():
	"""Manually flush both stall and visitor queues."""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("queue flush")
	if err:
		return err

	stalls_flushed, visitors_flushed = flush_registration_queue()
	return {
		"status": "success",
		"stalls_flushed": stalls_flushed,
		"visitors_flushed": visitors_flushed,
	}
