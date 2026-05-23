"""
Agent Event Handler - Handles agent events and thinking process output
"""

from common.log import logger


class AgentEventHandler:
    """
    Handles agent events and optionally sends intermediate messages to channel
    """
    
    def __init__(self, context=None, original_callback=None):
        """
        Initialize event handler
        
        Args:
            context: COW context (for accessing channel)
            original_callback: Original event callback to chain
        """
        self.context = context
        self.original_callback = original_callback
        
        # Get channel for sending intermediate messages
        self.channel = None
        if context:
            self.channel = context.kwargs.get("channel") if hasattr(context, "kwargs") else None
        self.progress_runtime = context.get("_session_runtime") if context else None
        
        self.current_content = ""
        self.turn_number = 0
    
    def handle_event(self, event):
        """
        Main event handler
        
        Args:
            event: Event dict with type and data
        """
        event_type = event.get("type")
        data = event.get("data", {})

        if self.progress_runtime:
            try:
                self.progress_runtime.update_progress(event_type, data)
            except Exception as e:
                logger.debug(f"[AgentEventHandler] Failed to update progress: {e}")
        
        # Dispatch to specific handlers
        if event_type == "turn_start":
            self._handle_turn_start(data)
        elif event_type == "message_update":
            self._handle_message_update(data)
        elif event_type == "message_end":
            self._handle_message_end(data)
        elif event_type == "reasoning_update":
            pass
        elif event_type == "tool_execution_start":
            self._handle_tool_execution_start(data)
        elif event_type == "tool_execution_end":
            self._handle_tool_execution_end(data)
        
        # Call original callback if provided
        if self.original_callback:
            self.original_callback(event)

    def _mark_visible_output(self, source):
        """Tell session runtime that user-visible output has been produced."""
        if not self.progress_runtime or not hasattr(self.progress_runtime, "mark_visible_output"):
            return

        try:
            self.progress_runtime.mark_visible_output(source)
        except Exception as e:
            logger.debug(f"[AgentEventHandler] Failed to mark visible output: {e}")
    
    def _handle_turn_start(self, data):
        """Handle turn start event"""
        self.turn_number = data.get("turn", 0)
        self.current_content = ""
    
    def _handle_message_update(self, data):
        """Handle message update event (streaming content text)"""
        delta = data.get("delta", "")
        self.current_content += delta
        if delta:
            self._mark_visible_output("message_update")
    
    def _handle_message_end(self, data):
        """Handle message end event"""
        tool_calls = data.get("tool_calls", [])
        
        if tool_calls:
            if self.current_content.strip():
                logger.info(f"💭 {self.current_content.strip()[:200]}{'...' if len(self.current_content) > 200 else ''}")
                self._send_to_channel(self.current_content.strip())
        else:
            if self.current_content.strip():
                logger.debug(f"💬 {self.current_content.strip()[:200]}{'...' if len(self.current_content) > 200 else ''}")
        
        self.current_content = ""
    
    def _handle_tool_execution_start(self, data):
        """Handle tool execution start event - logged by agent_stream.py"""
        pass
    
    def _handle_tool_execution_end(self, data):
        """Handle tool execution end event - logged by agent_stream.py"""
        pass
    
    def _send_to_channel(self, message):
        """
        Try to send intermediate message to channel.
        Skipped in SSE mode because thinking text is already streamed via on_event.
        """
        if self.context and self.context.get("on_event"):
            return

        if self.channel:
            try:
                from bridge.reply import Reply, ReplyType
                reply = Reply(ReplyType.TEXT, message)
                self._mark_visible_output("intermediate_send")
                self.channel._send(reply, self.context)
            except Exception as e:
                logger.debug(f"[AgentEventHandler] Failed to send to channel: {e}")
    
    def log_summary(self):
        """Log execution summary - simplified"""
        # Summary removed as per user request
        # Real-time logging during execution is sufficient
        pass
