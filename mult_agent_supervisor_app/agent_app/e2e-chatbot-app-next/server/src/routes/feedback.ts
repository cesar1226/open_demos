import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { authMiddleware, requireAuth } from '../middleware/auth';
import {
  getMessageById,
  voteMessage,
  updateVoteFeedbackText,
  getVotesByChatId,
} from '@chat-template/db';
import { checkChatAccess } from '@chat-template/core';
import { ChatSDKError } from '@chat-template/core/errors';
import { getDatabricksToken } from '@chat-template/auth';
import { getWorkspaceHostname } from '@chat-template/ai-sdk-providers';
import {
  getMessageMetadata,
  getAssessmentId,
  storeAssessmentId,
} from '../lib/message-meta-store';

export const feedbackRouter: RouterType = Router();

feedbackRouter.use(authMiddleware);

/**
 * POST /api/feedback - Submit feedback for a message
 *
 * Body:
 * - messageId: string - The ID of the message to provide feedback for
 * - feedbackType: 'thumbs_up' | 'thumbs_down' - The type of feedback
 */
feedbackRouter.post('/', requireAuth, async (req: Request, res: Response) => {
  try {
    const { messageId, feedbackType, feedbackText } = req.body;

    if (!messageId) {
      const error = new ChatSDKError('bad_request:api');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    if (!feedbackType && !feedbackText) {
      const error = new ChatSDKError('bad_request:api');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    if (feedbackType && feedbackType !== 'thumbs_up' && feedbackType !== 'thumbs_down') {
      const error = new ChatSDKError('bad_request:api');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    const session = req.session;
    if (!session) {
      const error = new ChatSDKError('unauthorized:chat');
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    // Get the message to retrieve traceId and chatId
    const messages = await getMessageById({ id: messageId });
    let traceId: string | null;
    let chatId: string | undefined;

    if (!messages || messages.length === 0) {
      // Fall back to in-memory store (ephemeral mode or DB unavailable)
      const metadata = getMessageMetadata(messageId);
      if (!metadata) {
        const error = new ChatSDKError('not_found:database');
        const response = error.toResponse();
        return res.status(response.status).json(response.json);
      }
      traceId = metadata.traceId;
      chatId = metadata.chatId;
    } else {
      const dbMessage = messages[0];
      traceId = dbMessage.traceId;
      chatId = dbMessage.chatId;
    }

    let mlflowAssessmentId: string | undefined;

    // Submit to MLflow if we have a trace ID
    if (traceId) {
      try {
        const token = await getDatabricksToken();
        const hostUrl = await getWorkspaceHostname();
        const userId = session.user.email ?? session.user.id;

        const findExistingAssessment = async (name: string): Promise<string | null> => {
          try {
            const r = await fetch(`${hostUrl}/api/3.0/mlflow/traces/${traceId}/assessments`, {
              headers: { Authorization: `Bearer ${token}` },
            });
            if (!r.ok) return null;
            const data = await r.json();
            const match = (data.assessments ?? []).find(
              (a: { assessment_name: string; source?: { source_id?: string }; assessment_id: string }) =>
                a.assessment_name === name && a.source?.source_id === userId,
            );
            return match?.assessment_id ?? null;
          } catch {
            return null;
          }
        };

        const submitAssessment = async (
          name: string,
          value: boolean | string,
          storeKey: string,
        ) => {
          const existingId =
            getAssessmentId(storeKey, session!.user.id) ??
            (await findExistingAssessment(name));
          const url = existingId
            ? `${hostUrl}/api/3.0/mlflow/traces/${traceId}/assessments/${existingId}`
            : `${hostUrl}/api/3.0/mlflow/traces/${traceId}/assessments`;
          const body = existingId
            ? { assessment: { trace_id: traceId, assessment_name: name, feedback: { value } }, update_mask: 'feedback' }
            : { assessment: { trace_id: traceId, assessment_name: name, source: { source_type: 'HUMAN', source_id: userId }, feedback: { value } } };

          const res = await fetch(url, {
            method: existingId ? 'PATCH' : 'POST',
            headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          if (!res.ok) throw new Error(await res.text());
          const result = await res.json();
          const id = result.assessment?.assessment_id;
          if (id) storeAssessmentId(storeKey, session!.user.id, id);
          return id;
        };

        if (feedbackType) {
          mlflowAssessmentId = await submitAssessment('user_feedback', feedbackType === 'thumbs_up', messageId);
        }
        if (feedbackText) {
          await submitAssessment('user_feedback_text', feedbackText, `${messageId}:text`);
        }
      } catch (error) {
        console.error('Error submitting feedback to MLflow:', error);
        const chatError = new ChatSDKError('offline:chat');
        const chatResponse = chatError.toResponse();
        return res.status(chatResponse.status).json(chatResponse.json);
      }
    } else {
      console.warn(
        'Message does not have a trace ID, skipping MLflow submission',
      );
    }

    // Persist thumbs to DB (upsert row with isUpvoted + optional text)
    if (chatId && feedbackType) {
      try {
        await voteMessage({
          chatId,
          messageId,
          type: feedbackType === 'thumbs_up' ? 'up' : 'down',
          ...(feedbackText && { feedbackText }),
        });
      } catch (err) {
        console.warn('[Feedback] DB vote save failed:', err);
      }
    } else if (chatId && feedbackText) {
      // Text-only submission: update feedbackText on the existing row
      try {
        await updateVoteFeedbackText({ chatId, messageId, feedbackText });
      } catch (err) {
        console.warn('[Feedback] DB feedbackText update failed:', err);
      }
    } else if (!chatId) {
      console.warn('[Feedback] DB write skipped — chatId not found for messageId:', messageId);
    }

    return res.status(200).json({
      success: true,
      mlflowAssessmentId,
    });
  } catch (error) {
    console.error('[Feedback] Error submitting feedback:', error);

    if (error instanceof ChatSDKError) {
      const response = error.toResponse();
      return res.status(response.status).json(response.json);
    }

    const chatError = new ChatSDKError('offline:chat');
    const response = chatError.toResponse();
    return res.status(response.status).json(response.json);
  }
});

/**
 * GET /api/feedback/chat/:chatId - Get all feedback for a chat
 *
 * Reads votes from the DB for fast single-query page load.
 * Returns a map of messageId -> feedback.
 * Returns empty map if DB is unavailable (graceful degradation).
 */
feedbackRouter.get(
  '/chat/:chatId',
  requireAuth,
  async (req: Request, res: Response) => {
    try {
      const { chatId } = req.params;
      const session = req.session;
      if (!session) {
        return res.status(200).json({});
      }

      // Ownership check: verify the chat belongs to the requesting user
      const { allowed, reason } = await checkChatAccess(
        chatId as string,
        session.user.email ?? session.user.id,
      );
      if (reason !== 'not_found' && !allowed) {
        return res.status(200).json({});
      }

      const dbVotes = await getVotesByChatId({ id: chatId as string });
      type Feedback = {
        messageId: string;
        feedbackType: 'thumbs_up' | 'thumbs_down';
        assessmentId: null;
      };
      const feedbackMap: Record<string, Feedback> = {};
      for (const v of dbVotes) {
        feedbackMap[v.messageId] = {
          messageId: v.messageId,
          feedbackType: v.isUpvoted ? 'thumbs_up' : 'thumbs_down',
          assessmentId: null,
        };
      }

      return res.status(200).json(feedbackMap);
    } catch (error) {
      console.error('[Feedback] Error getting feedback by chat:', error);
      if (error instanceof ChatSDKError) {
        const response = error.toResponse();
        return res.status(response.status).json(response.json);
      }
      const chatError = new ChatSDKError('offline:chat');
      const response = chatError.toResponse();
      return res.status(response.status).json(response.json);
    }
  },
);
