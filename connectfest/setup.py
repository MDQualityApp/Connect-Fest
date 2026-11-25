import frappe
from connectfest.services.stalls import setup_default_stalls
from connectfest.services.sponsors import setup_default_sponsors
from connectfest.services.terms import setup_default_terms


def setup_site_config_defaults():
	"""
	Automatically configures necessary site settings in site_config.json
	during `bench migrate` / `bench install-app` so manual site_config editing
	is never required on live/production servers.
	"""
	try:
		from frappe.installer import update_site_config
		site_conf = frappe.get_site_config()
		updates = {}
		if not site_conf.get("allow_cors"):
			updates["allow_cors"] = "*"
		if site_conf.get("ignore_csrf") is None:
			updates["ignore_csrf"] = True
		if not site_conf.get("throttle_user_limit"):
			updates["throttle_user_limit"] = 100000
		if site_conf.get("pause_scheduler"):
			updates["pause_scheduler"] = 0

		if updates:
			for key, val in updates.items():
				update_site_config(key, val)
			print(f"ConnectFest: Configured site defaults in site_config.json: {list(updates.keys())}")
	except Exception as e:
		frappe.log_error(f"ConnectFest setup_site_config_defaults error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest setup_site_config_defaults warning: {e}")

	try:
		frappe.db.set_single_value("System Settings", "enable_scheduler", 1)
	except Exception:
		pass


def ensure_standard_roles():
	"""Guarantees essential ConnectFest RBAC & scanner roles exist in tabRole without risking LinkValidationError."""
	roles_to_ensure = [
		("Website User", 0, 0),
		("ConnectFest Admin", 1, 1),
		("ConnectFest Gate Scanner", 0, 1),
		("ConnectFest Exhibitor Scanner", 0, 1),
		("ConnectFest Visitor Scanner", 0, 1),
		("ConnectFest Event Scanner", 0, 1),
		("ConnectFest Gallery Admin", 0, 1),
		("ConnectFest Stall Admin", 0, 1),
		("ConnectFest Sponsor Admin", 0, 1),
		("ConnectFest CNR Admin", 0, 1),
		("ConnectFest Terms Admin", 0, 1),
		("ConnectFest Dashboard Admin", 0, 1),
		("ConnectFest User Directory Admin", 0, 1),
	]
	try:
		for r_name, desk_acc, is_cust in roles_to_ensure:
			frappe.db.sql("""
				INSERT IGNORE INTO `tabRole` 
				(name, role_name, desk_access, is_custom, disabled, creation, modified, modified_by, owner) 
				VALUES (%s, %s, %s, %s, 0, NOW(), NOW(), 'Administrator', 'Administrator')
			""", (r_name, r_name, desk_acc, is_cust))
		frappe.db.commit()
		frappe.clear_cache(doctype="Role")
	except Exception:
		pass


def after_migrate():
	"""
	Hook executed automatically at the end of `bench migrate`.
	Ensures site_config defaults, pricing tiers, sponsors, terms, and settings exist.
	Stalls created by admin are preserved and never deleted or overwritten during migrate!
	"""
	ensure_standard_roles()
	setup_site_config_defaults()

	try:
		setup_default_stalls(preserve_booked=True, seed_stalls=False)
	except Exception as e:
		frappe.log_error(f"ConnectFest after_migrate error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_migrate warning: {e}")

	try:
		setup_default_sponsors()
	except Exception as e:
		frappe.log_error(f"ConnectFest after_migrate sponsors error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_migrate sponsors warning: {e}")

	try:
		setup_default_terms()
	except Exception as e:
		frappe.log_error(f"ConnectFest after_migrate terms error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_migrate terms warning: {e}")


def after_install():
	"""
	Hook executed automatically after the app is installed onto a site.
	"""
	ensure_standard_roles()
	setup_site_config_defaults()

	try:
		setup_default_stalls(preserve_booked=True, seed_stalls=False)
	except Exception as e:
		frappe.log_error(f"ConnectFest after_install error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_install warning: {e}")

	try:
		setup_default_sponsors()
	except Exception as e:
		frappe.log_error(f"ConnectFest after_install sponsors error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_install sponsors warning: {e}")

	try:
		setup_default_terms()
	except Exception as e:
		frappe.log_error(f"ConnectFest after_install terms error: {str(e)}", frappe.get_traceback())
		print(f"ConnectFest after_install terms warning: {e}")
