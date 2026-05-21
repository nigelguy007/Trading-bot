import { useState } from "react";
import { triggerRun } from "../lib/api";

export default function RunControls({ onRunStarted }) {
  const [loading, setLoading] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [bankroll, setBankroll] = useState(1000);
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);

  async function handleRun() {
    setLoading(true);
    setError(null);
    try {
      const result = await triggerRun({ dry_run: dryRun, bankroll });
      setLastResult(result);
      onRunStarted?.(result);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
      <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">
        Run Controls
      </h2>

      <div className="flex items-center gap-3">
        <label className="text-sm text-gray-400">Bankroll ($)</label>
        <input
          type="number"
          value={bankroll}
          onChange={(e) => setBankroll(Number(e.target.value))}
          className="w-28 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-sm text-white"
          min={100}
          step={100}
        />
      </div>

      <div className="flex items-center gap-3">
        <label className="relative inline-flex items-center cursor-pointer">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
            className="sr-only peer"
          />
          <div className="w-10 h-5 bg-gray-700 peer-checked:bg-brand-600 rounded-full transition-colors" />
          <div className="absolute left-1 top-0.5 w-4 h-4 bg-white rounded-full transition-transform peer-checked:translate-x-5" />
        </label>
        <span className="text-sm text-gray-400">
          {dryRun ? "Dry Run (simulated)" : "⚠️ LIVE trading"}
        </span>
      </div>

      {!dryRun && (
        <p className="text-xs text-red-400 bg-red-900/20 border border-red-800 rounded p-2">
          Warning: Live mode will execute real trades using your API keys and real funds.
        </p>
      )}

      <button
        onClick={handleRun}
        disabled={loading}
        className="w-full bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white font-semibold py-2 rounded-lg transition-colors text-sm"
      >
        {loading ? "Running..." : "Run Trading Cycle"}
      </button>

      {error && (
        <p className="text-xs text-red-400 bg-red-900/20 border border-red-800 rounded p-2">
          {error}
        </p>
      )}

      {lastResult && (
        <p className="text-xs text-green-400">
          Run started: {lastResult.run_id?.slice(0, 8)}...
        </p>
      )}
    </div>
  );
}
