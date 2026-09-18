import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { ChatHeader } from '@/components/chat-header';
import { DbIcon } from '@/components/ui/db-icon';
import { Input } from '@/components/ui/input';
import {
  AssistantIcon,
  SearchIcon,
  SparkleRectangleIcon,
} from '@/components/icons';
import type { AgentDef } from '@/lib/agents';
import { useAppConfig } from '@/contexts/AppConfigContext';

function AgentCard({
  agent,
  onClick,
}: { agent: AgentDef; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group flex h-full flex-col rounded-xl border border-border bg-background p-4 text-left transition-colors hover:border-primary/40 hover:bg-secondary/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="mb-4 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 className="truncate text-[15px] font-semibold text-foreground">
            {agent.name}
          </h4>
          <p className="text-xs text-muted-foreground">{agent.type}</p>
        </div>
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-secondary">
          <DbIcon
            icon={agent.kind === 'genie' ? SparkleRectangleIcon : AssistantIcon}
            size={18}
            color="ai"
          />
        </span>
      </div>
      <p className="line-clamp-3 text-sm text-muted-foreground">
        {agent.description}
      </p>
    </button>
  );
}

export default function AgentsPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const { agents, isLoading } = useAppConfig();

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter(
      (agent) =>
        agent.name.toLowerCase().includes(q) ||
        agent.type.toLowerCase().includes(q) ||
        agent.description.toLowerCase().includes(q),
    );
  }, [query, agents]);

  return (
    <>
      <ChatHeader />
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-5xl px-6 py-6">
          <h2 className="text-2xl font-semibold text-foreground">Agents</h2>

          <div className="relative mt-6">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2">
              <DbIcon icon={SearchIcon} size={16} color="muted" />
            </span>
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search agents..."
              className="h-11 rounded-xl pl-9"
            />
          </div>

          <h3 className="mt-8 mb-3 text-sm font-semibold text-muted-foreground">
            All agents
          </h3>

          {isLoading ? (
            <p className="py-12 text-center text-sm text-muted-foreground">
              Loading agents…
            </p>
          ) : agents.length === 0 ? (
            <p className="py-12 text-center text-sm text-muted-foreground">
              No Genie agents are configured. Set{' '}
              <code className="rounded bg-secondary px-1 py-0.5">
                GENIE_AGENTS
              </code>{' '}
              to add one.
            </p>
          ) : filtered.length === 0 ? (
            <p className="py-12 text-center text-sm text-muted-foreground">
              No agents match “{query}”.
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((agent) => (
                <AgentCard
                  key={agent.id}
                  agent={agent}
                  onClick={() => navigate(`/agents/${agent.id}`)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
