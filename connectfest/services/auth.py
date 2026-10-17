import frappe
from connectfest.services.common import extract_request_data


def ensure_frappe_user(email, full_name, mobile_no=None, role="Website User"):
	"""
	Ensures a Frappe User exists for visitors and exhibitors so they can log in.
	"""
	if not email or "@" not in email:
		return None
	clean_email = email.strip().lower()
	try:
		if not frappe.db.exists("User", clean_email):
			names = (full_name or clean_email.split("@")[0]).strip().split(" ", 1)
			first_name = names[0] or clean_email.split("@")[0]
			last_name = names[1] if len(names) > 1 else ""
			user = frappe.get_doc({
				"doctype": "User",
				"email": clean_email,
				"first_name": first_name,
				"last_name": last_name,
				"phone": mobile_no or "",
				"mobile_no": mobile_no or "",
				"enabled": 1,
				"send_welcome_email": 0,
				"user_type": "Website User",
				"roles": [{"doctype": "Has Role", "role": role}],
			})
			user.flags.ignore_permissions = True
			user.flags.ignore_password_policy = True
			user.insert(ignore_if_duplicate=True)
			frappe.db.commit()
			return user.name
		return clean_email
	except Exception as e:
		frappe.log_error("ConnectFest User Creation Error", str(e))
		return clean_email


def get_current_user_profile(**kwargs):
	"""
	Resolves current logged in user from either Frappe session cookie or Token Auth.
	If user is logged in, automatically returns their booked stall, invoice receipt, and digital pass.
	"""
	from connectfest.services.passes import get_my_dashboard
	try:
		data = extract_request_data(kwargs)
		sess_user = getattr(frappe.session, "user", None)
		email = None
		if sess_user and sess_user != "Guest":
			email = sess_user
		else:
			email = (data.get("email") or data.get("user_email") or "").strip().lower()

		if not email:
			return {
				"status": "guest",
				"is_logged_in": False,
				"user": "Guest",
				"message": "User is not logged in. Please log in to view your booked stalls and passes.",
			}

		return get_my_dashboard(email=email, **kwargs)
	except Exception as e:
		frappe.log_error("ConnectFest Profile Fetch Error", frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


def login_attendee(email=None, password=None, **kwargs):
	"""
	Allows attendees (Exhibitors and Visitors) to log in with credentials or email lookup.
	"""
	from connectfest.services.passes import get_my_dashboard
	try:
		data = extract_request_data(kwargs)
		usr = (email or data.get("email") or data.get("usr") or "").strip().lower()
		pwd = (password or data.get("pwd") or data.get("password") or "").strip()

		if not usr:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "Email address is required"}

		if pwd:
			try:
				frappe.auth.LoginManager().authenticate(usr, pwd)
				frappe.auth.LoginManager().post_login()
			except frappe.AuthenticationError:
				frappe.local.response["http_status_code"] = 401
				return {"status": "error", "message": "Invalid email or password"}

		return get_my_dashboard(email=usr)
	except Exception as e:
		frappe.log_error("ConnectFest Login Error", frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}
