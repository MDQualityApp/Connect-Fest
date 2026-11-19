from connectfest.services.stalls import setup_default_stalls
from connectfest.services.sponsors import setup_default_sponsors
from connectfest.services.terms import setup_default_terms


def execute():
	"""
	Migration patch executed during `bench migrate` (post model sync).
	Initializes dynamic tier pricing, default settings, sponsors, and terms.
	Does NOT overwrite or delete admin-created stalls.
	"""
	setup_default_stalls(preserve_booked=True, seed_stalls=False)
	setup_default_sponsors()
	setup_default_terms()
