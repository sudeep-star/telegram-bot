class MessageHandler:
    def __init__(self, bot):
        self.bot = bot

    def handle_message(self, update, context):
        message_text = update.message.text
        chat_id = update.message.chat_id
        
        # Process the incoming message and send a response
        response_text = f"You said: {message_text}"
        self.send_response(chat_id, response_text)

    def send_response(self, chat_id, text):
        self.bot.send_message(chat_id=chat_id, text=text)