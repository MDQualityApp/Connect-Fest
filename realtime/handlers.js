// ConnectFest Realtime Socket.IO Handlers for Frappe
const redis = require("redis");
const { get_redis_subscriber } = require("../../frappe/node_utils");

module.exports = function (socket) {
	// Join general website and stall update rooms
	socket.join("website");
	socket.join("all");
	socket.join("connectfest_stalls");

	// Handle client stall selection over Frappe Socket.IO
	socket.on("connectfest_select_stall", (data) => {
		try {
			if (!data || !data.stall_number) return;
			const payload = {
				event: "stall_selected",
				stall_number: data.stall_number,
				user_id: data.user_id || socket.user || "user",
				user_name: data.user_name || "User",
				timestamp: new Date().toISOString(),
			};
			socket.nsp.emit("connectfest_stall_update", payload);
		} catch (e) {
			console.error("ConnectFest socket selection error:", e);
		}
	});

	socket.on("connectfest_deselect_stall", (data) => {
		try {
			if (!data || !data.stall_number) return;
			const payload = {
				event: "stall_unselected",
				stall_number: data.stall_number,
				user_id: data.user_id || socket.user || "user",
				timestamp: new Date().toISOString(),
			};
			socket.nsp.emit("connectfest_stall_update", payload);
		} catch (e) {
			console.error("ConnectFest socket deselection error:", e);
		}
	});
};
