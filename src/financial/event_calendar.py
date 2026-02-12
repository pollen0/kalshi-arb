"""
Economic Event Calendar

Tracks high-impact economic events and pauses trading around them.
Events like FOMC, NFP, and CPI can cause large, unpredictable moves.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional
import json
import os


@dataclass
class EconomicEvent:
    """High-impact economic event"""
    name: str
    event_time: datetime
    impact: str  # "high", "medium", "low"
    currency: str  # "USD", "EUR", etc.
    description: str = ""

    # Trading pause settings
    pause_before_minutes: int = 30  # Pause trading X minutes before
    pause_after_minutes: int = 15   # Resume trading X minutes after


# Pre-defined major US economic events
# In production, this should be fetched from an API like Forex Factory or Investing.com
MAJOR_US_EVENTS = [
    # FOMC meetings are scheduled in advance
    ("FOMC Rate Decision", "high", 120, 30),  # 2 hours before, 30 min after
    ("FOMC Minutes", "high", 30, 15),
    ("Fed Chair Speech", "high", 30, 15),

    # Major economic indicators
    ("Nonfarm Payrolls", "high", 30, 15),
    ("CPI", "high", 30, 15),
    ("Core CPI", "high", 30, 15),
    ("PPI", "medium", 15, 10),
    ("Retail Sales", "medium", 15, 10),
    ("GDP", "high", 30, 15),
    ("Jobless Claims", "medium", 5, 5),
    ("PCE Price Index", "high", 30, 15),
    ("ISM Manufacturing", "medium", 15, 10),
    ("Consumer Confidence", "medium", 15, 10),

    # Market events
    ("Options Expiration", "medium", 60, 15),  # Triple/quad witching
    ("Market Open", "low", 5, 5),
    ("Market Close", "low", 5, 5),
]


class EventCalendar:
    """
    Manages economic event awareness for trading decisions.

    In production, this should:
    1. Fetch events from an economic calendar API
    2. Cache events locally
    3. Update daily
    """

    def __init__(self, events_file: str = None):
        self.events: List[EconomicEvent] = []

        if events_file and os.path.exists(events_file):
            self._load_events(events_file)

        # Add standard daily events
        self._add_daily_events()

    def _load_events(self, filepath: str):
        """Load events from JSON file"""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
                for evt in data.get("events", []):
                    self.events.append(EconomicEvent(
                        name=evt["name"],
                        event_time=datetime.fromisoformat(evt["time"]),
                        impact=evt.get("impact", "medium"),
                        currency=evt.get("currency", "USD"),
                        description=evt.get("description", ""),
                        pause_before_minutes=evt.get("pause_before", 30),
                        pause_after_minutes=evt.get("pause_after", 15),
                    ))
        except Exception as e:
            print(f"[CALENDAR] Error loading events: {e}")

    def _add_daily_events(self):
        """Add recurring daily events (market open/close)"""
        now = datetime.now(timezone.utc)
        today = now.date()

        # Market open (9:30 AM ET = 14:30 UTC during EST, 13:30 UTC during EDT)
        # Simplified: assume EST (14:30 UTC)
        market_open = datetime(today.year, today.month, today.day,
                               14, 30, tzinfo=timezone.utc)
        market_close = datetime(today.year, today.month, today.day,
                                21, 0, tzinfo=timezone.utc)

        # Add if not already in list
        has_open = any(e.name == "Market Open" and e.event_time.date() == today
                       for e in self.events)
        has_close = any(e.name == "Market Close" and e.event_time.date() == today
                        for e in self.events)

        if not has_open:
            self.events.append(EconomicEvent(
                name="Market Open",
                event_time=market_open,
                impact="low",
                currency="USD",
                description="US equity market opens",
                pause_before_minutes=5,
                pause_after_minutes=5,
            ))

        if not has_close:
            self.events.append(EconomicEvent(
                name="Market Close",
                event_time=market_close,
                impact="low",
                currency="USD",
                description="US equity market closes",
                pause_before_minutes=5,
                pause_after_minutes=5,
            ))

    def add_event(self, name: str, event_time: datetime, impact: str = "medium",
                  pause_before: int = 30, pause_after: int = 15):
        """Manually add an event"""
        self.events.append(EconomicEvent(
            name=name,
            event_time=event_time,
            impact=impact,
            currency="USD",
            pause_before_minutes=pause_before,
            pause_after_minutes=pause_after,
        ))

    def get_upcoming_events(self, hours: int = 24) -> List[EconomicEvent]:
        """Get events in the next N hours"""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours)

        return [e for e in self.events
                if now <= e.event_time <= cutoff]

    def get_active_pauses(self) -> List[EconomicEvent]:
        """Get events currently causing a trading pause"""
        now = datetime.now(timezone.utc)
        active = []

        for event in self.events:
            pause_start = event.event_time - timedelta(minutes=event.pause_before_minutes)
            pause_end = event.event_time + timedelta(minutes=event.pause_after_minutes)

            if pause_start <= now <= pause_end:
                active.append(event)

        return active

    def should_pause_trading(self) -> Tuple[bool, str]:
        """
        Check if trading should be paused due to an upcoming/active event.

        Returns:
            (should_pause, reason)
        """
        active_pauses = self.get_active_pauses()

        if not active_pauses:
            return (False, "")

        # Only pause for high-impact events, warn for medium
        high_impact = [e for e in active_pauses if e.impact == "high"]
        medium_impact = [e for e in active_pauses if e.impact == "medium"]

        if high_impact:
            event = high_impact[0]
            now = datetime.now(timezone.utc)
            if now < event.event_time:
                minutes_until = (event.event_time - now).total_seconds() / 60
                return (True, f"High-impact event '{event.name}' in {minutes_until:.0f} minutes")
            else:
                minutes_since = (now - event.event_time).total_seconds() / 60
                return (True, f"High-impact event '{event.name}' occurred {minutes_since:.0f} minutes ago")

        if medium_impact:
            event = medium_impact[0]
            return (False, f"Medium-impact event '{event.name}' - consider reducing position size")

        return (False, "")

    def get_volatility_multiplier(self) -> float:
        """
        Get volatility multiplier based on upcoming events.

        Use this to adjust expected volatility near events.
        """
        now = datetime.now(timezone.utc)
        multiplier = 1.0

        for event in self.events:
            # Check if event is within 4 hours
            time_to_event = (event.event_time - now).total_seconds() / 3600  # hours

            if 0 < time_to_event < 4:
                if event.impact == "high":
                    # Increase expected vol as event approaches
                    # At 4 hours: 1.2x, at 1 hour: 1.5x, at 15 min: 2.0x
                    event_multiplier = 1.0 + 0.5 * (4 - time_to_event) / 4
                    multiplier = max(multiplier, event_multiplier)
                elif event.impact == "medium":
                    event_multiplier = 1.0 + 0.2 * (4 - time_to_event) / 4
                    multiplier = max(multiplier, event_multiplier)

            elif -1 < time_to_event <= 0:
                # Just after event - highest volatility
                if event.impact == "high":
                    multiplier = max(multiplier, 2.0)
                elif event.impact == "medium":
                    multiplier = max(multiplier, 1.3)

        return multiplier

    def format_upcoming(self, hours: int = 24) -> str:
        """Format upcoming events for display"""
        events = self.get_upcoming_events(hours)
        if not events:
            return "No major events in the next 24 hours"

        lines = ["Upcoming Economic Events:"]
        for event in sorted(events, key=lambda e: e.event_time):
            time_str = event.event_time.strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"  [{event.impact.upper()}] {event.name} @ {time_str}")

        return "\n".join(lines)


# Global calendar instance
_calendar: Optional[EventCalendar] = None


def get_event_calendar() -> EventCalendar:
    """Get or create global event calendar"""
    global _calendar
    if _calendar is None:
        _calendar = EventCalendar()
    return _calendar


# FOMC dates for 2024-2025 (update as needed)
# These are the major high-impact events to track
FOMC_DATES_2024_2025 = [
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-11-05", "2025-12-17",
]


def load_fomc_dates(calendar: EventCalendar):
    """Load FOMC meeting dates into calendar"""
    for date_str in FOMC_DATES_2024_2025:
        try:
            # FOMC announcements are typically at 2:00 PM ET (19:00 UTC)
            event_time = datetime.strptime(date_str + " 19:00", "%Y-%m-%d %H:%M")
            event_time = event_time.replace(tzinfo=timezone.utc)

            # Only add future events
            if event_time > datetime.now(timezone.utc):
                calendar.add_event(
                    name="FOMC Rate Decision",
                    event_time=event_time,
                    impact="high",
                    pause_before=120,  # 2 hours before
                    pause_after=30,    # 30 minutes after
                )
        except ValueError:
            pass
