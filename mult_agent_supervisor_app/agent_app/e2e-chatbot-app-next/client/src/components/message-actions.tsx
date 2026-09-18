import { useCopyToClipboard } from 'usehooks-ts';

import { Actions, Action } from './elements/actions';
import { memo, useState, useCallback, useRef, useEffect } from 'react';
import { toast } from 'sonner';
import type { ChatMessage, Feedback } from '@chat-template/core';
import { useAppConfig } from '@/contexts/AppConfigContext';
import {
  ChevronDown,
  ChevronUp,
  MessageSquareText,
} from 'lucide-react';
import { DbIcon } from './ui/db-icon';
import { PencilIcon, CopyIcon, ThumbsUpIcon, ThumbsDownIcon } from './icons';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from './ui/alert-dialog';
import { Textarea } from './ui/textarea';

function PureMessageActions({
  message,
  isLoading,
  setMode,
  errorCount = 0,
  showErrors = false,
  onToggleErrors,
  initialFeedback,
}: {
  message: ChatMessage;
  isLoading: boolean;
  setMode?: (mode: 'view' | 'edit') => void;
  errorCount?: number;
  showErrors?: boolean;
  onToggleErrors?: () => void;
  initialFeedback?: Feedback;
}) {
  const { feedbackEnabled } = useAppConfig();
  const [_, copyToClipboard] = useCopyToClipboard();
  const [feedback, setFeedback] = useState<'thumbs_up' | 'thumbs_down' | null>(
    initialFeedback?.feedbackType || null,
  );
  const [feedbackText, setFeedbackText] = useState(
    initialFeedback?.feedbackText || '',
  );
  const [isTextDialogOpen, setIsTextDialogOpen] = useState(false);
  const [draftFeedbackText, setDraftFeedbackText] = useState('');
  const isSubmittingRef = useRef(false);

  useEffect(() => {
    if (initialFeedback?.feedbackType && feedback === null) {
      setFeedback(initialFeedback.feedbackType);
    }
  }, [initialFeedback?.feedbackType, feedback]);

  useEffect(() => {
    if (initialFeedback?.feedbackText && !feedbackText) {
      setFeedbackText(initialFeedback.feedbackText);
    }
  }, [initialFeedback?.feedbackText, feedbackText]);

  const textFromParts = message.parts
    ?.filter((part) => part.type === 'text')
    .map((part) => part.text)
    .join('\n')
    .trim();

  const traceIdPart = message.parts?.find((p) => p.type === 'data-traceId') as
    | { type: 'data-traceId'; data: string | null }
    | undefined;
  const feedbackSupported = traceIdPart === undefined || traceIdPart.data !== null;

  const submitFeedback = useCallback(
    async ({
      feedbackType,
      text,
    }: {
      feedbackType?: 'thumbs_up' | 'thumbs_down';
      text?: string;
    }) => {
      if (isSubmittingRef.current) return;
      isSubmittingRef.current = true;

      try {
        const response = await fetch('/api/feedback', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            messageId: message.id,
            ...(feedbackType ? { feedbackType } : {}),
            ...(text ? { feedbackText: text } : {}),
          }),
        });

        if (!response.ok) {
          throw new Error('Failed to submit feedback');
        }

        if (feedbackType) {
          setFeedback(feedbackType);
        }
        if (text) {
          setFeedbackText(text);
        }
      } catch (error) {
        console.error('Error submitting feedback:', error);
        toast.error(
          'Failed to submit feedback. Please try again, or contact the app developer if the error persists.',
        );
        throw error;
      } finally {
        isSubmittingRef.current = false;
      }
    },
    [message.id],
  );

  const handleFeedback = useCallback(
    async (feedbackType: 'thumbs_up' | 'thumbs_down') => {
      await submitFeedback({ feedbackType });
    },
    [submitFeedback],
  );

  const openTextFeedbackDialog = useCallback(() => {
    setDraftFeedbackText(feedbackText);
    setIsTextDialogOpen(true);
  }, [feedbackText]);

  const handleTextFeedbackSubmit = useCallback(async () => {
    const trimmed = draftFeedbackText.trim();
    if (!trimmed) {
      toast.error('Please enter feedback before submitting.');
      return;
    }

    try {
      await submitFeedback({ text: trimmed });
      setIsTextDialogOpen(false);
      toast.success('Feedback submitted.');
    } catch {
      // submitFeedback already shows an error toast
    }
  }, [draftFeedbackText, submitFeedback]);

  const handleCopy = useCallback(async () => {
    if (!textFromParts) {
      toast.error("There's no text to copy!");
      return;
    }

    await copyToClipboard(textFromParts);
    toast.success('Copied to clipboard!');
  }, [textFromParts, copyToClipboard]);

  if (isLoading) return null;

  if (message.role === 'user') {
    return (
      <Actions className="-mr-0.5 justify-end">
        <div className="relative flex items-center gap-1">
          {setMode && (
            <Action
              tooltip="Edit"
              onClick={() => setMode('edit')}
              className="opacity-0 transition-opacity group-hover/message:opacity-100"
              data-testid="message-edit-button"
            >
              <DbIcon icon={PencilIcon} />
            </Action>
          )}
          <Action tooltip="Copy" onClick={handleCopy}>
            <DbIcon icon={CopyIcon} />
          </Action>
        </div>
      </Actions>
    );
  }

  const feedbackButtons = (
    <>
      <Action
        tooltip="Thumbs up"
        onClick={() => handleFeedback('thumbs_up')}
        className={feedback === 'thumbs_up' ? 'text-green-600' : ''}
        data-testid="thumbs-up-button"
      >
        <DbIcon icon={ThumbsUpIcon} />
      </Action>
      <Action
        tooltip="Thumbs down"
        onClick={() => handleFeedback('thumbs_down')}
        className={feedback === 'thumbs_down' ? 'text-red-600' : ''}
        data-testid="thumbs-down-button"
      >
        <DbIcon icon={ThumbsDownIcon} />
      </Action>
      <Action
        tooltip={feedbackText ? 'Edit text feedback' : 'Add text feedback'}
        onClick={openTextFeedbackDialog}
        className={feedbackText ? 'text-blue-600' : ''}
        data-testid="text-feedback-button"
      >
        <MessageSquareText className="h-4 w-4" />
      </Action>
    </>
  );

  return (
    <>
      <Actions className="-ml-0.5">
        {textFromParts && (
          <Action tooltip="Copy" onClick={handleCopy}>
            <CopyIcon />
          </Action>
        )}
        {feedbackEnabled && feedbackSupported && feedbackButtons}
        {errorCount > 0 && onToggleErrors && (
          <Action
            tooltip={showErrors ? 'Hide errors' : 'Show errors'}
            onClick={onToggleErrors}
            iconOnly={false}
          >
            <div className="flex items-center gap-1.5">
              {showErrors ? <ChevronUp /> : <ChevronDown />}
              <span className="text-xs">
                {errorCount} {errorCount === 1 ? 'error' : 'errors'}
              </span>
            </div>
          </Action>
        )}
      </Actions>

      <AlertDialog open={isTextDialogOpen} onOpenChange={setIsTextDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Share feedback</AlertDialogTitle>
            <AlertDialogDescription>
              Tell us what worked or what could be improved for this response.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <Textarea
            value={draftFeedbackText}
            onChange={(event) => setDraftFeedbackText(event.target.value)}
            placeholder="What was helpful or missing?"
            rows={4}
            data-testid="text-feedback-input"
          />
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleTextFeedbackSubmit}>
              Submit feedback
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export const MessageActions = memo(
  PureMessageActions,
  (prevProps, nextProps) => {
    if (prevProps.isLoading !== nextProps.isLoading) return false;
    if (prevProps.errorCount !== nextProps.errorCount) return false;
    if (prevProps.showErrors !== nextProps.showErrors) return false;
    if (prevProps.initialFeedback?.feedbackType !== nextProps.initialFeedback?.feedbackType)
      return false;
    if (prevProps.initialFeedback?.feedbackText !== nextProps.initialFeedback?.feedbackText)
      return false;
    const prevTraceId = prevProps.message.parts?.find(
      (p) => p.type === 'data-traceId',
    );
    const nextTraceId = nextProps.message.parts?.find(
      (p) => p.type === 'data-traceId',
    );
    if (prevTraceId !== nextTraceId) return false;

    return true;
  },
);
