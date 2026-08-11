"""
Interactive setup wizard for first-time configuration.
"""

import sys
from config import Config
from telegram_handler import TelegramHandler


class SetupWizard:
    """Interactive setup wizard"""

    def __init__(self, config_path: str = "config.json"):
        self.config = Config(config_path)

    def run(self) -> bool:
        """Run the setup wizard"""
        print("\n" + "=" * 60)
        print("Welcome to Reddit to Telegram Bot Setup!")
        print("=" * 60)
        print("\nThis wizard will help you configure your bot.\n")

        # Step 1: Bot token
        if not self.setup_bot_token():
            return False

        # Step 2: Admin ID
        if not self.setup_admin_id():
            return False

        # Step 3: Channel
        if not self.setup_channel():
            return False

        # Step 4: Subreddits
        if not self.setup_subreddits():
            return False

        # Step 5: Reddit API
        self.setup_reddit_api()

        # Step 6: Schedule
        self.setup_schedule()

        # Step 7: Filters (optional)
        self.setup_filters()

        # Save config
        if self.config.save():
            print("\n" + "=" * 60)
            print("Configuration saved successfully!")
            print("=" * 60)
            print("\nYou can now run the bot with: python main.py")
            print("\nImportant reminders:")
            print("1. Make sure the bot is an admin in your channel")
            print("2. Start a chat with your bot and send /start")
            print("3. Keep config.json private (it contains your bot token)")
            print("\n")
            return True
        else:
            print("\nFailed to save configuration")
            return False

    def setup_bot_token(self) -> bool:
        """Setup bot token"""
        print("Step 1: Bot Token")
        print("-" * 60)
        print("To create a bot:")
        print("1. Open Telegram and search for @BotFather")
        print("2. Send: /newbot")
        print("3. Follow instructions and copy your bot token")
        print()

        while True:
            token = input("Enter your bot token: ").strip()

            if not token:
                print("Token cannot be empty\n")
                continue

            # Test token
            print("Testing bot token...")
            telegram = TelegramHandler(token)
            valid, result = telegram.test_bot_token()

            if valid:
                print(f"Bot token valid! Bot username: @{result}\n")
                self.config.bot_token = token
                return True
            else:
                print(f"Invalid bot token: {result}")
                retry = input("Try again? (y/n): ").strip().lower()
                if retry != "y":
                    return False

    def setup_admin_id(self) -> bool:
        """Setup admin chat ID"""
        print("\nStep 2: Admin ID")
        print("-" * 60)
        print("To get your Telegram ID:")
        print("1. Open Telegram and search for @userinfobot")
        print("2. Send any message to it")
        print("3. Copy your ID number")
        print()

        while True:
            admin_id = input("Enter your Telegram ID: ").strip()

            try:
                admin_id = int(admin_id)

                # Test messaging
                print("Testing admin messaging...")
                telegram = TelegramHandler(self.config.bot_token)
                valid, error = telegram.test_can_message_admin(admin_id)

                if valid:
                    print("Successfully sent test message to admin!\n")
                    self.config.admin_chat_id = admin_id
                    return True
                else:
                    print(f"Cannot message admin: {error}")
                    print("\nMake sure you:")
                    print("1. Started a chat with your bot")
                    print("2. Sent /start or any message to it")

                    retry = input("\nTry again? (y/n): ").strip().lower()
                    if retry != "y":
                        return False

            except ValueError:
                print("Please enter a valid numeric ID\n")

    def setup_channel(self) -> bool:
        """Setup channel"""
        print("\nStep 3: Channel")
        print("-" * 60)
        print("To setup your channel:")
        print("1. Create a public channel in Telegram")
        print("2. Add your bot as an admin with post permissions")
        print("3. Enter the channel username (with @)")
        print()

        while True:
            channel = input("Enter channel username (e.g., @mychannel): ").strip()

            if not channel:
                print("Channel cannot be empty\n")
                continue

            if not channel.startswith("@"):
                channel = "@" + channel

            print(f"Channel set to: {channel}")
            print("Remember to make your bot an admin in this channel!\n")

            self.config.channels = [{"username": channel, "description": "Main channel"}]
            return True

    def setup_subreddits(self) -> bool:
        """Setup subreddits"""
        print("\nStep 4: Subreddits")
        print("-" * 60)
        print("Choose content source:\n")

        presets = {
            "1": {
                "name": "Cats",
                "subs": [
                    "supermodelcats",
                    "sillycats",
                    "catsoncats",
                    "CatsBeingAdorable",
                    "cutecats",
                    "catsareliquid",
                    "funnycats",
                    "CatsWithDogs",
                ],
            },
            "2": {
                "name": "Dogs",
                "subs": [
                    "rarepuppers",
                    "dogpictures",
                    "puppies",
                    "lookatmydog",
                    "corgi",
                    "goldenretrievers",
                    "pitbulls",
                    "beagle",
                ],
            },
            "3": {
                "name": "Memes",
                "subs": [
                    "memes",
                    "dankmemes",
                    "wholesomememes",
                    "AdviceAnimals",
                    "MemeEconomy",
                    "BikiniBottomTwitter",
                    "PrequelMemes",
                    "me_irl",
                ],
            },
            "4": {
                "name": "Nature",
                "subs": [
                    "EarthPorn",
                    "natureporn",
                    "SkyPorn",
                    "WaterPorn",
                    "botanicalporn",
                    "ruralporn",
                    "winterporn",
                    "AutumnPorn",
                ],
            },
            "5": {"name": "Custom", "subs": []},
        }

        for key, preset in presets.items():
            print(f"{key}. {preset['name']}")

        choice = input("\nSelect option (1-5): ").strip()

        if choice in presets:
            preset = presets[choice]

            if choice == "5":  # Custom
                print("\nEnter subreddit names (without r/), one per line.")
                print("Press Enter on empty line when done:\n")

                subs = []
                while True:
                    sub = input("Subreddit: ").strip()
                    if not sub:
                        break
                    subs.append(sub)

                if not subs:
                    print("No subreddits entered")
                    return False

                self.config.subreddits = subs
            else:
                self.config.subreddits = preset["subs"]
                print(f"\nSelected {len(preset['subs'])} subreddits from {preset['name']}")

            return True
        else:
            print("Invalid choice")
            return False

    def setup_reddit_api(self) -> None:
        """Offer app-only Reddit OAuth setup."""
        print("\nStep 5: Reddit API")
        print("-" * 60)
        print("Reddit can block anonymous API traffic on some networks.")
        print("App-only OAuth is recommended for reliable subreddit fetching.")
        print("Create a script app at: https://www.reddit.com/prefs/apps")
        print()

        configure_now = input("Configure Reddit OAuth now? (y/n, default: y): ").strip().lower()
        if configure_now == "n":
            print("Skipping Reddit OAuth setup. Anonymous Reddit requests may be blocked.\n")
            return

        while True:
            client_id = input("Reddit client ID (leave blank to skip): ").strip()
            if not client_id:
                print("Skipping Reddit OAuth setup. Anonymous Reddit requests may be blocked.\n")
                return

            client_secret = input("Reddit client secret: ").strip()
            if not client_secret:
                print("Client secret cannot be empty when client ID is set.\n")
                continue

            username = input("Reddit username for User-Agent (without /u/): ").strip()
            username = username.removeprefix("/u/").strip().strip("/")
            if not username:
                print("A Reddit username is required to build a descriptive User-Agent.\n")
                continue

            self.config.user_agent = f"windows:telegram-autoposter:v2.0 (by /u/{username})"
            self.config.reddit_client_id = client_id
            self.config.reddit_client_secret = client_secret
            print("Reddit OAuth settings saved.\n")
            return

    def setup_schedule(self) -> None:
        """Setup posting schedule"""
        print("\nStep 6: Posting Schedule")
        print("-" * 60)

        # Post interval
        while True:
            interval = input("Post interval in minutes (default: 45): ").strip()

            if not interval:
                self.config.post_interval_minutes = 45
                break

            try:
                interval = int(interval)
                if interval < 1:
                    print("Interval must be at least 1 minute")
                    continue
                self.config.post_interval_minutes = interval
                break
            except ValueError:
                print("Please enter a valid number")
        # Active hours
        active = input("\nLimit posting to specific hours? (y/n, default: n): ").strip().lower()

        if active == "y":
            print("\nEnter active hours in 24-hour format (HH:MM)")
            start = input("Start time (e.g., 08:00): ").strip() or "08:00"
            end = input("End time (e.g., 23:00): ").strip() or "23:00"

            self.config.active_hours_enabled = True
            self.config.active_hours_start = start
            self.config.active_hours_end = end
            print(f"Active hours: {start} - {end}")
        else:
            self.config.active_hours_enabled = False
            print("Bot will post 24/7")

        # Daily post limit
        print()
        while True:
            raw_limit = input("Max posts per day (0 = unlimited, default: 32): ").strip()

            if not raw_limit:
                self.config.daily_post_limit = 32
                break

            try:
                limit = int(raw_limit)
            except ValueError:
                print("Please enter a valid number")
                continue

            if limit < 0:
                print("Limit cannot be negative")
                continue

            self.config.daily_post_limit = limit
            break

        if self.config.daily_post_limit == 0:
            print("Daily post limit: unlimited")
        else:
            print(f"Daily post limit: {self.config.daily_post_limit} posts/day")

        print()

    def setup_filters(self) -> None:
        """Setup content filters"""
        print("\nStep 7: Content Filters (Optional)")
        print("-" * 60)

        filters = input("Configure content filters? (y/n, default: n): ").strip().lower()

        if filters != "y":
            print("Using default filters\n")
            return

        # Minimum upvotes
        upvotes = input("\nMinimum upvotes (default: 0): ").strip()
        if upvotes:
            try:
                self.config.min_upvotes = int(upvotes)
            except ValueError:
                pass

        # Skip NSFW
        nsfw = input("Skip NSFW content? (y/n, default: y): ").strip().lower()
        self.config.skip_nsfw = nsfw != "n"

        # Minimum image width
        width = input("Minimum image width in pixels (default: 800): ").strip()
        if width:
            try:
                self.config.min_image_width = int(width)
            except ValueError:
                pass

        print("Filters configured\n")


def main():
    """Run setup wizard"""
    wizard = SetupWizard()

    try:
        success = wizard.run()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nSetup cancelled")
        sys.exit(1)
    except Exception as e:
        print(f"\nSetup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
