# Running an event leaderboard

An event runs one or more games, and every (event, game) pair gets its own leaderboard, so
several events can run on the same dates. You never need a developer or a deploy to set one up.

## Set up a new event (2 minutes)

0. *Once per game:* in the **Games** tab, give each game a **play URL** (where players start it,
   e.g. `https://my-game.dev/?event={event}`) and, if it runs on an arcade cabinet, a
   **cabinet URL**. `{event}` is replaced with the event's slug; leave it out and
   `?event=<slug>` is added. Disk Invaders already has `https://inkless.dev/e/{event}`.
1. Open the score server's **`/admin`** page and sign in, then click the **Events** tab
   (or go straight to **`/admin#events`**).
2. Fill in the form at the top:
   - **slug**: a short lowercase id with hyphens and a year, e.g. `kafka-summit-2027`
     (it appears in links, so keep it tidy; the year stops next year's event mixing in)
   - **name**: what players see, e.g. "Kafka Summit 2027"
   - **opens / closes** (optional, UTC): plays outside the window still save, but are not
     put on the event's board. Leave blank for always open.
   - **games**: tick every game being played there (at least one).
   - **CTA button text / CTA link** (optional): after a player claims a score at this event,
     the claim page shows this button instead of "Learn More About Inkless". Set both or
     neither; leave them blank for the Inkless default.
3. Click **Save event**. The event gets one row per game, each with everything you need:

   | You want | Use |
   |---|---|
   | Players on their own phones (slide deck, poster) | **Game link** or its **QR code** (the game's play URL with the event in it) |
   | A big screen or a "see where you rank" slide | **Leaderboard link** or its QR code (`/leaderboard?game=<game>&event=<slug>`) |
   | An arcade cabinet | **Cabinet URL** or its QR code: set the cabinet browser's start page to it, once |

   A link (and its QR code) only appears once the game has that URL set in the Games tab.

   QR codes download as PNG or SVG (SVG stays sharp on a projector).

## During and after the event

- The **leaderboard page** has game and event pickers (it only offers events that run the chosen
  game); `…/leaderboard?game=<game>&event=<slug>` links straight to one board.
- In `/admin`, on the **Scores** tab, use the **event filter** to see just that event's entrants. **Export CSV**
  follows the filter, so the prize draw uses that event's list only.
- Junk or test entries: **Edit** or delete them in the table before drawing.

## Good to know

- A game is tagged with its event when it is *played*, from the link it was started from.
  Nothing is inferred from dates, which is why two events can overlap.
- A typo'd or unknown event slug does no harm: the score is saved with no event and shows on
  no event board. The same goes for a score from a game the event doesn't run.
- Removing a game from an event only stops *new* plays of it being tagged with the event. Scores
  already tagged stay on that game's board for the event (delete them in the Scores tab if needed).
- Anyone who knows an event's link can play into it. For a raffle, draw from the CSV and
  remove stragglers first.
- Nicknames only have to be unique within an event.
