/**
 * Types + helpers for the "Agents" tab.
 *
 * The agent list is NOT hardcoded here — it is derived at runtime from the
 * Genie spaces the app is configured with (the `GENIE_AGENTS` env var, the same
 * source of truth the orchestrator uses for its Genie tools). The server reads
 * that config and returns the resolved list (with computed embed URLs) from
 * `GET /api/config`, which the UI reads via `useAppConfig().agents`.
 *
 * To add or change an agent, update `GENIE_AGENTS` (see `.env.example`) — no
 * frontend change is needed.
 */

export type AgentKind = 'genie' | 'agent';

export interface AgentDef {
  /** Stable URL slug used in the route (/agents/:id). */
  id: string;
  /** Display name shown on the card and header. */
  name: string;
  /** Short type label shown under the name (e.g. "AI/BI Genie"). */
  type: string;
  /** One-line description shown on the card. */
  description: string;
  /** Kind of agent — drives the card badge/icon. */
  kind: AgentKind;
  /** URL loaded into the embedded iframe when the agent is opened. */
  embedUrl: string;
}

export function getAgentById(
  agents: AgentDef[] | undefined,
  id: string | undefined,
): AgentDef | undefined {
  if (!agents || !id) return undefined;
  return agents.find((agent) => agent.id === id);
}
