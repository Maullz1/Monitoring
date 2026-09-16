import os
import logging
import requests
from datetime import timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("itc-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")

INTERFACES = {
    "ether2": "(ISP1)",
}

DHCP_SERVERS = {
    "dhcp-mgmt": "Management",
}

GNC_INTERFACES = {
    "ether2": "(ISP1)",
}

GNC_DHCP_SERVERS = {
    "dhcp1": "Management",
}

# routerboard_name persis seperti yang keluar di label metrik mktxp
ROUTERS = {
    "": ,
}

# Peta interface & DHCP per router (kunci = routerboard_name)
ROUTER_INTERFACES = {
    "ITC": INTERFACES,
    "GNC": GNC_INTERFACES,
}
ROUTER_DHCP = {
    "ITC": DHCP_SERVERS,
    "GNC": GNC_DHCP_SERVERS,
}


def prom_query(expr: str):
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": expr},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data["status"] != "success":
            return None
        return [(item["metric"], float(item["value"][1])) for item in data["data"]["result"]]
    except Exception as e:
        log.error(f"Prometheus query failed: {e}")
        return None


def fmt_bps(bytes_per_sec: float) -> str:
    bits = bytes_per_sec * 8
    if bits >= 1_000_000:
        return f"{bits / 1_000_000:.2f} Mbps"
    if bits >= 1_000:
        return f"{bits / 1_000:.1f} Kbps"
    return f"{bits:.0f} bps"


def fmt_bytes(b: float) -> str:
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.2f} GiB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MiB"
    return f"{b / 1024:.0f} KiB"


def fmt_uptime(seconds: float) -> str:
    td = timedelta(seconds=int(seconds))
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}h")
    if hours:
        parts.append(f"{hours}j")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot monitoring ITC & Gancit aktif.\n\n"
        "Perintah tersedia:\n"
        "/status - status up/down + uptime, ITC & Gancit\n"
        "/rtt - latency (avg & P90) semua ISP\n"
        "/cpu - CPU load & memory, ITC & Gancit\n"
        "/packetloss - packet loss semua ISP\n"
        "/traffic - traffic RX/TX per interface, ITC & Gancit\n"
        "/users - device aktif per VLAN, ITC & Gancit\n"
        "/errors - error/drop paket per interface, ITC & Gancit"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🖥️ *Status Monitoring*\n"]

    mktxp_up = prom_query('up{job="mktxp"}')
    smoke_up = prom_query('up{job="smokeping"}')

    mktxp_ok = mktxp_up and mktxp_up[0][1] == 1
    smoke_ok = smoke_up and smoke_up[0][1] == 1

    lines.append(f"{'🟢' if mktxp_ok else '🔴'} mktxp (exporter data router): {'UP' if mktxp_ok else 'DOWN'}")
    lines.append(f"{'🟢' if smoke_ok else '🔴'} smokeping (exporter ping ISP): {'UP' if smoke_ok else 'DOWN'}\n")

    for rb_name, label in ROUTERS.items():
        uptime = prom_query(f'mktxp_system_uptime{{routerboard_name="{rb_name}"}}')
        if uptime:
            router_labels, value = uptime[0]
            lines.append(
                f"📟 *{label}* ({router_labels.get('board_name', '')})\n"
                f"  RouterOS {router_labels.get('version', '')} — Uptime: {fmt_uptime(value)}"
            )
        else:
            lines.append(f"📟 *{label}*: ⚠️ data tidak tersedia (cek koneksi ke router ini)")

    await update.message.reply_markdown("\n".join(lines))


async def cmd_rtt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📶 *Latency ISP (5 menit terakhir)*\n"]

    avg_result = prom_query(
        "sum(rate(smokeping_response_duration_seconds_sum[5m])) by (name) "
        "/ sum(rate(smokeping_response_duration_seconds_count[5m])) by (name)"
    )
    p90_result = prom_query(
        "histogram_quantile(0.90, sum(rate(smokeping_response_duration_seconds_bucket[5m])) by (le, name))"
    )

    if not avg_result:
        await update.message.reply_text("Data smokeping tidak tersedia.")
        return

    p90_map = {labels.get("name"): value for labels, value in (p90_result or [])}

    for labels, avg_val in avg_result:
        name = labels.get("name", "unknown")
        p90_val = p90_map.get(name)
        avg_ms = avg_val * 1000
        p90_ms = p90_val * 1000 if p90_val is not None else None
        emoji = "🟢" if avg_ms < 50 else ("🟡" if avg_ms < 150 else "🔴")
        p90_str = f"{p90_ms:.1f} ms" if p90_ms is not None else "N/A"
        lines.append(f"{emoji} *{name}*\n  avg: {avg_ms:.1f} ms   P90: {p90_str}")

    await update.message.reply_markdown("\n".join(lines))


async def cmd_cpu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["⚙️ *CPU & Memory Router*\n"]

    for rb_name, label in ROUTERS.items():
        cpu = prom_query(f'mktxp_system_cpu_load{{routerboard_name="{rb_name}"}}')
        free_mem = prom_query(f'mktxp_system_free_memory{{routerboard_name="{rb_name}"}}')
        total_mem = prom_query(f'mktxp_system_total_memory{{routerboard_name="{rb_name}"}}')

        lines.append(f"*{label}*")

        if cpu:
            cpu_val = cpu[0][1]
            emoji = "🟢" if cpu_val < 60 else ("🟡" if cpu_val < 85 else "🔴")
            lines.append(f"{emoji} CPU Load: {cpu_val:.1f}%")
        else:
            lines.append("⚠️ Data CPU tidak tersedia.")

        if free_mem and total_mem:
            free_val = free_mem[0][1]
            total_val = total_mem[0][1]
            used_val = total_val - free_val
            used_pct = (used_val / total_val) * 100
            emoji = "🟢" if used_pct < 70 else ("🟡" if used_pct < 90 else "🔴")
            lines.append(
                f"{emoji} Memory: {fmt_bytes(used_val)} / {fmt_bytes(total_val)} ({used_pct:.1f}%)\n"
            )
        else:
            lines.append("⚠️ Data memory tidak tersedia.\n")

    await update.message.reply_markdown("\n".join(lines))


async def cmd_traffic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 *Traffic (rata-rata 1 menit terakhir)*\n"]
    for rb_name, router_label in ROUTERS.items():
        interfaces = ROUTER_INTERFACES[rb_name]
        lines.append(f"━━ *{router_label}* ━━")
        found_any = False
        for iface, label in interfaces.items():
            rx = prom_query(f'rate(mktxp_interface_rx_byte_total{{name="{iface}",routerboard_name="{rb_name}"}}[1m])')
            tx = prom_query(f'rate(mktxp_interface_tx_byte_total{{name="{iface}",routerboard_name="{rb_name}"}}[1m])')
            rx_val = rx[0][1] if rx else None
            tx_val = tx[0][1] if tx else None
            if rx_val is None and tx_val is None:
                continue
            found_any = True
            rx_str = fmt_bps(rx_val) if rx_val is not None else "N/A"
            tx_str = fmt_bps(tx_val) if tx_val is not None else "N/A"
            lines.append(f"*{label}*\n  ⬇ RX: {rx_str}   ⬆ TX: {tx_str}")
        if not found_any:
            lines.append("⚠️ Data tidak tersedia.")
    await update.message.reply_markdown("\n".join(lines))


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["👥 *Active DHCP Leases per VLAN*\n"]
    for rb_name, router_label in ROUTERS.items():
        dhcp_servers = ROUTER_DHCP[rb_name]
        lines.append(f"━━ *{router_label}* ━━")
        total = 0
        found_any = False
        for server, label in dhcp_servers.items():
            result = prom_query(f'mktxp_dhcp_lease_active_count{{server="{server}",routerboard_name="{rb_name}"}}')
            if not result:
                continue
            found_any = True
            count = int(result[0][1])
            total += count
            lines.append(f"• {label}: {count} device")
        if found_any:
            lines.append(f"*Total {router_label}: {total} device aktif*")
        else:
            lines.append("⚠️ Data tidak tersedia.")
    await update.message.reply_markdown("\n".join(lines))


async def cmd_packetloss(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📉 *Packet Loss ISP (5 menit terakhir)*\n"]
    result = prom_query(
        "(sum(rate(smokeping_requests_total[5m])) by (name) "
        "- sum(rate(smokeping_response_duration_seconds_count[5m])) by (name)) "
        "/ sum(rate(smokeping_requests_total[5m])) by (name) * 100"
    )
    if not result:
        await update.message.reply_text("Data smokeping tidak tersedia.")
        return
    for labels, value in result:
        name = labels.get("name", "unknown")
        emoji = "🟢" if value < 2 else ("🟡" if value < 10 else "🔴")
        lines.append(f"{emoji} {name}: {value:.2f}%")
    await update.message.reply_markdown("\n".join(lines))


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["⚠️ *Interface Errors/Drops (rate per detik, 5 menit terakhir)*\n"]
    for rb_name, router_label in ROUTERS.items():
        interfaces = ROUTER_INTERFACES[rb_name]
        lines.append(f"━━ *{router_label}* ━━")
        found_any = False
        for iface, label in interfaces.items():
            rx_err = prom_query(f'rate(mktxp_interface_rx_error_total{{name="{iface}",routerboard_name="{rb_name}"}}[5m])')
            tx_err = prom_query(f'rate(mktxp_interface_tx_error_total{{name="{iface}",routerboard_name="{rb_name}"}}[5m])')
            rx_drop = prom_query(f'rate(mktxp_interface_rx_drop_total{{name="{iface}",routerboard_name="{rb_name}"}}[5m])')
            tx_drop = prom_query(f'rate(mktxp_interface_tx_drop_total{{name="{iface}",routerboard_name="{rb_name}"}}[5m])')

            rx_err_v = rx_err[0][1] if rx_err else 0
            tx_err_v = tx_err[0][1] if tx_err else 0
            rx_drop_v = rx_drop[0][1] if rx_drop else 0
            tx_drop_v = tx_drop[0][1] if tx_drop else 0

            if max(rx_err_v, tx_err_v, rx_drop_v, tx_drop_v) < 0.001:
                continue
            found_any = True
            lines.append(
                f"*{label}*\n"
                f"  error rx/tx: {rx_err_v:.3f}/{tx_err_v:.3f} pkt/s\n"
                f"  drop rx/tx: {rx_drop_v:.3f}/{tx_drop_v:.3f} pkt/s"
            )
        if not found_any:
            lines.append("✅ Tidak ada error/drop signifikan.")
    await update.message.reply_markdown("\n".join(lines))


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("rtt", cmd_rtt))
    app.add_handler(CommandHandler("cpu", cmd_cpu))
    app.add_handler(CommandHandler("traffic", cmd_traffic))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("packetloss", cmd_packetloss))
    app.add_handler(CommandHandler("errors", cmd_errors))
    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
