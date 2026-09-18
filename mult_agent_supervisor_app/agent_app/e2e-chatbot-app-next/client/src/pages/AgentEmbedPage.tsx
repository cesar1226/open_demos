import { useNavigate, useParams } from 'react-router-dom';

import { SidebarToggle } from '@/components/sidebar-toggle';
import { Button } from '@/components/ui/button';
import { DbIcon } from '@/components/ui/db-icon';
import { ArrowLeftIcon, NewWindowIcon } from '@/components/icons';
import { getAgentById } from '@/lib/agents';
import { useAppConfig } from '@/contexts/AppConfigContext';

export default function AgentEmbedPage() {
  const navigate = useNavigate();
  const { agentId } = useParams<{ agentId: string }>();
  const { agents, isLoading } = useAppConfig();
  const agent = getAgentById(agents, agentId);

  if (!agent) {
    // Config may still be loading on a hard refresh / deep link.
    if (isLoading) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
          <p className="text-muted-foreground">Loading agent…</p>
        </div>
      );
    }

    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
        <p className="text-muted-foreground">This agent could not be found.</p>
        <Button variant="secondary" onClick={() => navigate('/agents')}>
          Back to agents
        </Button>
      </div>
    );
  }

  return (
    <>
      <header className="sticky top-0 flex h-[60px] items-center gap-2 border-b border-border bg-background px-4">
        <div className="md:hidden">
          <SidebarToggle forceOpenIcon />
        </div>

        <Button
          variant="tertiary"
          size="sm"
          className="h-8 gap-1.5 px-2"
          onClick={() => navigate('/agents')}
        >
          <DbIcon icon={ArrowLeftIcon} size={16} />
          <span className="hidden sm:inline">Agents</span>
        </Button>

        <div className="min-w-0">
          <h4 className="truncate text-[16px] font-medium">{agent.name}</h4>
        </div>

        <a
          href={agent.embedUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="ml-auto flex items-center gap-1.5 rounded-lg border border-border bg-muted px-2 py-1 text-xs text-foreground hover:text-foreground"
        >
          <DbIcon icon={NewWindowIcon} size={14} color="muted" />
          <span className="hidden sm:inline">Open in new tab</span>
        </a>
      </header>

      <div className="flex-1 overflow-hidden">
        <iframe
          key={agent.id}
          src={agent.embedUrl}
          title={agent.name}
          className="h-full w-full border-0"
          allow="clipboard-write"
        />
      </div>
    </>
  );
}
