import clsx from "clsx";

function EdgeBadge({ edge }) {
  if (edge == null) return <span className="text-gray-600">—</span>;
  const pct = (edge * 100).toFixed(1);
  const color =
    edge > 0.1 ? "text-green-400" : edge > 0.05 ? "text-yellow-400" : "text-gray-400";
  return <span className={clsx("font-mono font-semibold", color)}>{pct}%</span>;
}

function PlatformBadge({ platform }) {
  const colors = {
    polymarket: "bg-blue-900 text-blue-300",
    kalshi: "bg-purple-900 text-purple-300",
  };
  return (
    <span
      className={clsx(
        "px-2 py-0.5 rounded text-xs font-medium uppercase",
        colors[platform] || "bg-gray-800 text-gray-400"
      )}
    >
      {platform}
    </span>
  );
}

export default function MarketTable({ markets = [] }) {
  if (!markets.length)
    return (
      <div className="text-center py-16 text-gray-500">
        No markets found. Run a scan to populate.
      </div>
    );

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-800 text-left text-gray-400 text-xs uppercase">
            <th className="pb-3 pr-4">Platform</th>
            <th className="pb-3 pr-4">Question</th>
            <th className="pb-3 pr-4 text-right">Yes Price</th>
            <th className="pb-3 pr-4 text-right">Model Prob</th>
            <th className="pb-3 pr-4 text-right">Edge</th>
            <th className="pb-3 pr-4 text-right">Liquidity</th>
            <th className="pb-3 text-right">Days Left</th>
          </tr>
        </thead>
        <tbody>
          {markets.map((m, i) => (
            <tr
              key={m.market_id || i}
              className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors"
            >
              <td className="py-3 pr-4">
                <PlatformBadge platform={m.platform} />
              </td>
              <td className="py-3 pr-4 max-w-xs">
                <span className="line-clamp-2 text-gray-200">{m.question}</span>
              </td>
              <td className="py-3 pr-4 text-right font-mono">
                {m.yes_price != null ? (m.yes_price * 100).toFixed(1) + "¢" : "—"}
              </td>
              <td className="py-3 pr-4 text-right font-mono text-brand-500">
                {m.model_prob != null ? (m.model_prob * 100).toFixed(1) + "%" : "—"}
              </td>
              <td className="py-3 pr-4 text-right">
                <EdgeBadge edge={m.edge} />
              </td>
              <td className="py-3 pr-4 text-right text-gray-300">
                {m.liquidity != null ? "$" + Number(m.liquidity).toLocaleString() : "—"}
              </td>
              <td className="py-3 text-right text-gray-400">
                {m.time_to_resolution_days != null
                  ? Math.round(m.time_to_resolution_days) + "d"
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
