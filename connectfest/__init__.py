__version__ = "0.0.1"

# Pre-warm weasyprint at server boot to eliminate 2+ second cold import latency during runtime requests
try:
	import weasyprint
except Exception:
	pass
