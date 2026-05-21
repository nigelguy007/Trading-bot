import clsx from "clsx";

function StatusBadge({ status }) {
  const colors = {
    filled: "bg-green-900 text-green-300",
    rejected: "bg-red-900 text-red-300",
    error: "bg-red-900 text-red-400",
    partial: "bg-yellow-900 text-yellow-300",
  };
  return (
    <span
      className={clsx(
        "px-2 py-0.5 rounded text-xs font-medium",
        colors[status] || "bg-gray-800 text-gray-400"
      )}
    >
      {status}
    </span>
  );
}

export default function TradeTable({ trades = [] }) {
  if (!trades.length)
    return (
      <div className="text-center py-16 text-gray-500">
        No trades yet. Trigger a run to start.
      </div>
    );

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-800 text-left text-gray-400 text-xs uppercase">
            <th className="pb-3 pr-4">Status</th>
            <th className="pb-3 pr-4">Platform</th>
            <th className="pb-3 pr-4">Market</th>
            <th className="pb-3 pr-4">Dir</th>
            <th className="pb-3 pr-4 text-right">Size</th>
            <th className="pb-3 pr-4 text-right">Fill</th>
            <th className="pb-3 pr-4 text-right">Edge</th>
            <th className="pb-3 text-right">PnL</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => (
            <tr
              key={t.order_id || i}
              className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors"
            >
              <td className="py-3 pr-4">
                <StatusBadge status={t.status} />
              </td>
              <td className="py-3 pr-4 text-xs text-gray-400 uppercase">{t.platform}</td>
              <td className="py-3 pr-4 max-w-[200px]">
                <span className="line-clamp-1 text-gray-200 text-xs">{t.market_id}</span>
              </td>
              <td className="py-3 pr-4">
                <span
                  className={clsx(
                    "font-bold text-xs",
                    t.direction === "yes" ? "text-green-400" : "text-red-400"
                  )}
                >
                  {t.direction?.toUpperCase()}
                </span>
              </td>
              <td className="py-3 pr-4 text-right font-mono">
                ${t.size_usd?.toFixed(2)}
              </td>
              <td className="py-3 pr-4 text-right font-mono text-gray-300">
                {t.fill_price != null ? (t.fill_price * 100).toFixed(1) + "¢" : "—"}
              </td>
              <td className="py-3 pr-4 text-right font-mono text-yellow-400">
                {t.edge != null ? (t.edge * 100).toFixed(1) + "%" : "—"}
              </td>
              <td className="py-3 text-right font-mono">
                <span
                  className={clsx(
                    t.pnl_realized > 0
                      ? "text-green-400"
                      : t.pnl_realized < 0
                      ? "text-red-400"
                      : "text-gray-500"
                  )}
                >
                  ${t.pnl_realized?.toFixed(2) ?? "0.00"}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
