# Telegram Bot Project

This project is a simple Telegram bot that can send messages. It is built using Python and the `python-telegram-bot` library.

## Project Structure

```
telegram-bot-project
├── bot
│   ├── __init__.py
│   ├── main.py
│   ├── handlers
│   │   ├── __init__.py
│   │   └── message_handler.py
│   └── utils
│       ├── __init__.py
│       └── helper.py
├── requirements.txt
├── .env
└── README.md
```

## Setup Instructions

1. Clone the repository:
   ```
   git clone <repository-url>
   cd telegram-bot-project
   ```

2. Create a virtual environment:
   ```
   python -m venv venv
   ```

3. Activate the virtual environment:
   - On Windows:
     ```
     venv\Scripts\activate
     ```
   - On macOS/Linux:
     ```
     source venv/bin/activate
     ```

4. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

5. Create a `.env` file in the root directory and add your Telegram bot token:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   ```

## Usage

To run the bot, execute the following command:
```
python bot/main.py
```

The bot will start listening for messages and respond accordingly.

## Contributing

Feel free to submit issues or pull requests if you have suggestions or improvements for the project.