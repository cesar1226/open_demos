import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { isDatabaseAvailable } from '@chat-template/db';
import { getEndpointOboInfo } from '@chat-template/ai-sdk-providers';

export const configRouter: RouterType = Router();

/**
 * A Genie space surfaced in the "Agents" tab, derived from the configured ids.
 * Mirrors the client-side `AgentDef` shape so the UI can render it directly.
 */
interface GenieAgent {
  /** Stable URL slug used in the route (/agents/:id). */
  id: string;
  /** Display name shown on the card and header. */
  name: string;
  /** Short type label shown under the name. */
  type: string;
  /** One-line description shown on the card. */
  description: string;
  kind: 'genie';
  /** Absolute URL loaded into the embedded iframe. */
  embedUrl: string;
}

interface GenieSpec {
  id: string;
  name?: string;
  label?: string;
  description?: string;
}

/**
 * Parse the configured Genie ids from the environment, mirroring the Python
 * agent server's contract (see agent_server/genie_agent.py):
 *   1. GENIE_AGENTS     — JSON array of {id, name?, label?, description?} (or bare id strings)
 *   2. GENIE_AGENT_IDS  — comma-separated space ids
 *   3. GENIE_SPACE_ID   — a single space id
 * De-duplicates by id, keeping the first occurrence. This is the single source
 * of truth shared with the orchestrator, so the Agents tab always reflects the
 * Genie spaces the app is actually configured with.
 */
function parseGenieSpecs(): GenieSpec[] {
  const specs: GenieSpec[] = [];

  const raw = process.env.GENIE_AGENTS;
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        for (const entry of parsed) {
          if (typeof entry === 'string' && entry.trim()) {
            specs.push({ id: entry.trim() });
          } else if (entry && typeof entry === 'object' && entry.id) {
            specs.push({
              id: String(entry.id).trim(),
              name: entry.name,
              label: entry.label,
              description: entry.description,
            });
          }
        }
      }
    } catch {
      console.warn('[config] GENIE_AGENTS is not valid JSON; ignoring it.');
    }
  }

  if (specs.length === 0) {
    const ids = process.env.GENIE_AGENT_IDS || '';
    for (const id of ids
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)) {
      specs.push({ id });
    }
  }

  if (specs.length === 0 && process.env.GENIE_SPACE_ID?.trim()) {
    specs.push({ id: process.env.GENIE_SPACE_ID.trim() });
  }

  const seen = new Set<string>();
  return specs.filter((s) => {
    if (!s.id || seen.has(s.id)) return false;
    seen.add(s.id);
    return true;
  });
}

/** Normalize a workspace host into an absolute origin with no trailing slash. */
function normalizeHost(raw: string | undefined): string | undefined {
  if (!raw?.trim()) return undefined;
  let host = raw.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//.test(host)) host = `https://${host}`;
  return host;
}

/** Turn a snake/kebab machine name into a Title Case display label. */
function prettifyName(name: string): string {
  return name
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Build the "Agents" list from the configured Genie ids. Embed URLs are derived
 * from the workspace host (GENIE_EMBED_HOST override, else DATABRICKS_HOST which
 * Databricks Apps injects automatically) and an optional org id (DATABRICKS_ORG_ID,
 * the `?o=` parameter). Returns [] when no host is resolvable so the UI can show
 * an empty state rather than broken iframes.
 */
function getGenieAgents(): GenieAgent[] {
  const host = normalizeHost(
    process.env.GENIE_EMBED_HOST || process.env.DATABRICKS_HOST,
  );
  if (!host) return [];

  const orgId = process.env.DATABRICKS_ORG_ID?.trim();
  const usedSlugs = new Set<string>();

  return parseGenieSpecs().map((spec) => {
    const displayName =
      spec.label?.trim() ||
      (spec.name?.trim()
        ? prettifyName(spec.name)
        : `Genie ${spec.id.slice(0, 8)}`);

    // Stable, unique route slug derived from the display name.
    const base =
      displayName
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '') || 'genie';
    let slug = base;
    let n = 2;
    while (usedSlugs.has(slug)) slug = `${base}-${n++}`;
    usedSlugs.add(slug);

    const embedUrl = orgId
      ? `${host}/embed/genie/rooms/${spec.id}?o=${encodeURIComponent(orgId)}`
      : `${host}/embed/genie/rooms/${spec.id}`;

    return {
      id: slug,
      name: displayName,
      type: 'AI/BI Genie',
      description: spec.description?.trim() || 'AI/BI Genie space.',
      kind: 'genie' as const,
      embedUrl,
    };
  });
}

/**
 * Extract OAuth scopes from a JWT token (without verification).
 * Databricks tokens use 'scope' (space-separated string) or 'scp' (array).
 */
function getScopesFromToken(token: string): string[] {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return [];
    const payload = JSON.parse(
      Buffer.from(parts[1], 'base64url').toString('utf-8'),
    );
    if (typeof payload.scope === 'string') return payload.scope.split(' ');
    if (Array.isArray(payload.scp)) return payload.scp as string[];
    return [];
  } catch {
    return [];
  }
}

/**
 * GET /api/config - Get application configuration
 * Returns feature flags, the empty-state greeting, and OBO status based on
 * environment configuration.
 * If the user's OBO token is present, decodes it to check which required
 * scopes are missing — the banner only shows missing scopes.
 */
configRouter.get('/', async (req: Request, res: Response) => {
  const oboInfo = await getEndpointOboInfo();

  let missingScopes = oboInfo.endpointRequiredScopes;

  // If the user has an OBO token, check which scopes are already present
  const userToken = req.headers['x-forwarded-access-token'] as
    | string
    | undefined;
  if (userToken && oboInfo.isEndpointOboEnabled) {
    const tokenScopes = getScopesFromToken(userToken);
    // A required scope like "sql.statement-execution" is satisfied by
    // an exact match OR by its parent prefix (e.g. "sql")
    missingScopes = oboInfo.endpointRequiredScopes.filter((required) => {
      const parent = required.split('.')[0];
      return !tokenScopes.some((ts) => ts === required || ts === parent);
    });
  }

  res.json({
    features: {
      chatHistory: isDatabaseAvailable(),
      feedback: !!process.env.MLFLOW_EXPERIMENT_ID,
    },
    greeting: process.env.CHAT_GREETING || undefined,
    agents: getGenieAgents(),
    obo: {
      missingScopes,
    },
  });
});
