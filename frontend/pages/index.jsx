import { useState } from "react";
import useSWR from "swr";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid
} from "recharts";
import { fetcher } from "../lib/api";
import StatCard from "../components/StatCard";
import MarketTable from "../components/MarketTable";
import TradeTable from "../components/TradeTable";
import RunControls from "../components/RunControls";

const TABS = ["Markets", "Trades", "Runs"];

function Nav({ active, setActive }) {
  return (
    <nav className="flex gap-1 bg-gray-900 border border-gray-800 rounded-lg p-1 w-fit">
      {TABS.map((t) => (
        <button
          key={t}
          onClick={() => setActive(t)}
          className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
            active === t
              ? "bg-brand-600 text-white"
              : "text-gray-400 hover:text-white"
          }`}
        >
          {t}
        </button>
      ))}
    </nav>
  );
}

export default function Dashboard() {
  const [tab, setTab] = useState("Markets");
  const [refreshKey, setRefreshKey] = useState(0);

  const { data: summary } = useSWR("/dashboard/summary", fetcher, {
    refreshInterval: 15000,
  });
  const { data: markets } = useSWR(
    tab === "Markets" ? "/markets/?limit=50" : null,
    fetcher,
    { refreshInterval: 30000 }
  );
  const { data: trades } = useSWR(
    tab === "Trades" ? "/trades/?limit=100" : null,
    fetcher,
    { refreshInterval: 15000 }
  );
  const { data: runs } = useSWR(
    tab === "Runs" ? "/dashboard/runs?limit=20" : null,
    fetcher,
    { refreshInterval: 20000 }
  );

  const pnl = summary?.total_pnl_usd ?? 0;
  const pnlColor = pnl > 0 ? "text-green-400" : pnl < 0 ? "text-red-400" : "text-white";

  return (
    <div className="min-h-screen bg-gray-950">
      {/* Header */}
      <header className="border-b border-gray-800 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-brand-600 rounded-lg flex items-center justify-center text-white font-bold text-sm">
            P
          </div>
          <div>
            <h1 className="font-bold text-white text-lg leading-none">
              Prediction Market Bot
            </h1>
            <p className="text-xs text-gray-500">Claude-powered trading agent</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
          <span className="text-xs text-gray-400">Live</span>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-8 space-y-8">
        {/* Stats Row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard
            label="Total PnL"
            value={`$${pnl.toFixed(2)}`}
            color={pnlColor}
            sub="Realized"
          />
          <StatCard
            label="Trades Filled"
            value={summary?.total_trades ?? "—"}
            sub="All time"
          />
          <StatCard
            label="Volume"
            value={`$${(summary?.total_volume_usd ?? 0).toLocaleString()}`}
            sub="Deployed"
          />
          <StatCard
            label="Markets w/ Edge"
            value={summary?.markets_with_edge ?? "—"}
            sub="Last scan"
          />
        </div>

        {/* Last Run Banner */}
        {summary?.last_run && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-3 flex flex-wrap gap-6 text-sm">
            <span className="text-gray-500">Last run:</span>
            <span className="text-gray-300">
              {new Date(summary.last_run.started_at).toLocaleString()}
            </span>
            <span className="text-gray-400">
              Scanned <strong className="text-white">{summary.last_run.markets_scanned}</strong> markets
            </span>
            <span className="text-gray-400">
              Filled <strong className="text-green-400">{summary.last_run.trades_filled}</strong> trades
            </span>
            {summary.last_run.dry_run && (
              <span className="px-2 py-0.5 bg-yellow-900 text-yellow-300 text-xs rounded">
                DRY RUN
              </span>
            )}
          </div>
        )}

        {/* Main Content */}
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
          {/* Left sidebar */}
          <div className="lg:col-span-1">
            <RunControls
              onRunStarted={() => setTimeout(() => setRefreshKey((k) => k + 1), 2000)}
            />
          </div>

          {/* Main panel */}
          <div className="lg:col-span-3 space-y-4">
            <Nav active={tab} setActive={setTab} />

            <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
              {tab === "Markets" && <MarketTable markets={markets ?? []} />}
              {tab === "Trades" && <TradeTable trades={trades ?? []} />}
              {tab === "Runs" && <RunsTable runs={runs ?? []} />}
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

function RunsTable({ runs }) {
  if (!runs.length)
    return (
      <div className="text-center py-16 text-gray-500">No runs yet.</div>
    );

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-gray-800 text-left text-gray-400 text-xs uppercase">
            <th className="pb-3 pr-4">Run ID</th>
            <th className="pb-3 pr-4">Started</th>
            <th className="pb-3 pr-4 text-right">Scanned</th>
            <th className="pb-3 pr-4 text-right">Opportunities</th>
            <th className="pb-3 pr-4 text-right">Filled</th>
            <th className="pb-3 text-right">Mode</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr
              key={r.run_id}
              className="border-b border-gray-800/50 hover:bg-gray-800/30"
            >
              <td className="py-3 pr-4 font-mono text-xs text-gray-400">
                {r.run_id?.slice(0, 8)}...
              </td>
              <td className="py-3 pr-4 text-xs text-gray-300">
                {r.started_at ? new Date(r.started_at).toLocaleString() : "—"}
              </td>
              <td className="py-3 pr-4 text-right">{r.total_scanned}</td>
              <td className="py-3 pr-4 text-right text-yellow-400">
                {r.opportunities_found}
              </td>
              <td className="py-3 pr-4 text-right text-green-400">
                {r.trades_filled}
              </td>
              <td className="py-3 text-right">
                <span
                  className={`px-2 py-0.5 rounded text-xs ${
                    r.dry_run
                      ? "bg-yellow-900 text-yellow-300"
                      : "bg-green-900 text-green-300"
                  }`}
                >
                  {r.dry_run ? "Dry" : "Live"}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
