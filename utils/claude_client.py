"""
utils/claude_client.py
──────────────────────
Wrapper around the Anthropic Python SDK.

All Claude API calls in the project go through this module so that
model selection, error handling, and JSON parsing live in one place.

Public functions
----------------
get_daily_trivia_era()  → tuple[str, str]
    Return the (era_key, era_label) pair for today's date-derived trivia
    era rotation.

generate_trivia_question(sports, era_label, avoid_questions)  → dict
    Generate a multiple-choice sports trivia question.

describe_release(artist, title, genre)  → str
    Write a short hype blurb for a Spotify release to include in the
    weekly music post.

summarize_pitchfork_review(artist, album, review_text)  → str
    Summarize a Pitchfork album review into 3–4 sentences for the
    monthly album review embed.

summarize_criterion_film(title, director, year, overview)  → str
    Write a 2–3 sentence movie night pitch for a Criterion Collection film.
"""

import json
import logging
import re
from datetime import date

import anthropic

import config

logger = logging.getLogger(__name__)

# Instantiate the client once at module load. The Anthropic client is
# stateless and thread-safe, so a single instance is fine for the whole bot.
_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# claude-haiku is the fastest and cheapest Claude model — well suited for
# these short-output tasks where latency matters more than deep reasoning.
_MODEL = "claude-haiku-4-5-20251001"


# Rotate through four eras day by day using the ordinal of today's date. This
# avoids relying on Claude to "randomly" select an era — LLMs have strong
# training-data biases (e.g. always landing on the 1974 World Cup) that make
# self-selected "randomness" unreliable across daily calls.
#
# Each entry is (era_key, era_label): era_key is a short stable identifier
# used to tag and look up TriviaQuestionPost history (see
# get_daily_trivia_era() and cogs/trivia.py); era_label is the full
# descriptive text injected into the prompt below. Keeping them separate
# means editing the prompt wording for an era doesn't invalidate existing
# history rows keyed on era_key.
_ERAS: list[tuple[str, str]] = [
    ("pre_1970", "Pre-1970 (early history, founding era, rule changes, legendary pioneers)"),
    ("1970s_1990s", "1970s–1990s (classic era — but avoid over-covered events like the 1974 World Cup; focus on lesser-known moments, records, and figures from this period)"),
    ("2000s_2010s", "2000s–2010s (modern era)"),
    ("2015_present", "2015–present (recent era — transfer fees, contract details, recent records, current rosters)"),
]


def get_daily_trivia_era() -> tuple[str, str]:
    """
    Return the (era_key, era_label) pair for today's date-derived era rotation.

    Callers use era_key to filter TriviaQuestionPost history (so the avoid-list
    passed to generate_trivia_question only contains questions from the same
    era) and era_label as the era argument to generate_trivia_question itself.
    """
    index = date.today().toordinal() % len(_ERAS)
    return _ERAS[index]


def generate_trivia_question(
    sports: list[str],
    era_label: str,
    avoid_questions: list[str] | None = None,
) -> dict:
    """
    Ask Claude to generate one multiple-choice sports trivia question.

    The prompt instructs Claude to return ONLY a JSON object so we can parse
    it reliably without further text extraction.

    Parameters
    ----------
    sports : list[str]
        Sport names Claude can draw from, e.g. ["soccer", "baseball"].
    era_label : str
        The era_label half of get_daily_trivia_era()'s return value — the
        full descriptive era text injected into the prompt as a mandatory
        constraint.
    avoid_questions : list[str] | None
        Question text from recently posted trivia in this same era (see
        cogs/trivia.py, which queries TriviaQuestionPost filtered by era_key).
        Passed to Claude as an explicit avoid-list so the rotation doesn't
        keep regenerating the same "greatest hits" fact every time this era
        comes back around. None or empty means no history exists yet.

    Returns
    -------
    dict with keys:
        question    – the trivia question string
        options     – {"A": "...", "B": "...", "C": "...", "D": "..."}
        correct     – the letter of the correct option ("A"–"D")
        explanation – one or two sentences explaining the answer
        sport       – which sport the question is about

    Raises
    ------
    ValueError
        If Claude returns something that cannot be parsed as valid JSON or is
        missing expected keys. The caller should handle this gracefully.
    """
    sports_str = ", ".join(sports)

    # Rendered as its own paragraph right after the era requirement so
    # Claude sees "here's the era, and here's specifically what not to
    # repeat within it" as one coherent instruction.
    avoid_section = ""
    if avoid_questions:
        avoid_block = "\n".join(f"- {q}" for q in avoid_questions)
        avoid_section = f"""

AVOID REPEATING — you already asked these questions recently during this same era. Write about a different fact, record, event, or figure than any of them, even if reworded:
{avoid_block}"""

    prompt = f"""You are a sports trivia expert writing questions for a group of dedicated, knowledgeable fans.

Generate one hard multiple-choice trivia question about one of these sports: {sports_str}.

ERA REQUIREMENT — this is mandatory, not optional: today's era is:

  {era_label}

You MUST write a question from this era. Do not substitute a different era.
{avoid_section}

The audience follows these sports closely, so avoid anything a casual fan would know. Good question topics include:
- Specific records, statistics, or milestones (e.g. exact numbers, career totals, single-season marks)
- Rule changes, founding moments, or defunct teams/leagues
- Draft history, trades, and roster moves that serious followers would recall
- Awards and honours beyond the most famous ones
- Coaches, referees, or front-office figures rather than just star players
- Unusual or lesser-known moments in playoff/championship history
- Country-level or club-level records from any era
- Recent transfer fees, contract details, or roster facts (for the 2015–present era)

Avoid: the very most famous moments every casual fan knows (e.g. the Miracle on Ice, Brazil 1970, etc.), or questions that require no specialized knowledge.

Return ONLY a valid JSON object — no markdown fences, no explanation text — with this exact structure:
{{
    "question": "The trivia question here?",
    "options": {{
        "A": "First option",
        "B": "Second option",
        "C": "Third option",
        "D": "Fourth option"
    }},
    "correct": "A",
    "explanation": "One or two sentences explaining the correct answer.",
    "sport": "The sport this question is about"
}}

Rules:
- Exactly one option must be correct.
- Use real, verifiable facts only — no invented statistics.
- Make the wrong answer options plausible enough that even knowledgeable fans might second-guess themselves.
- The "sport" field must be one of: {sports_str}."""

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Claude occasionally wraps JSON in markdown code fences despite the prompt
    # telling it not to. Strip them defensively.
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Claude returned non-JSON trivia response: %s", raw)
        raise ValueError(f"Could not parse Claude trivia response as JSON: {exc}") from exc

    # Validate the expected keys are all present before returning.
    required = {"question", "options", "correct", "explanation", "sport"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Claude trivia response is missing keys: {missing}")

    return data


def describe_release(artist: str, title: str, genre: str) -> str:
    """
    Ask Claude to write a short, engaging blurb for a new music release.

    This is used in the weekly music post to give each Spotify release a
    human-feeling sentence or two rather than just showing the raw metadata.

    Parameters
    ----------
    artist : str   The artist name as returned by Spotify.
    title  : str   The album/single title as returned by Spotify.
    genre  : str   The genre category (e.g. "indie rock").

    Returns
    -------
    str
        A 1–2 sentence description ready to embed in the Discord message.
    """
    prompt = f"""Write a 1–2 sentence hype blurb for a Discord music bot announcing this new release.

Artist: {artist}
Title: {title}
Genre: {genre}

Keep it casual, enthusiastic, and informative. No hashtags. Return only the blurb text — no labels, no preamble."""

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()


def summarize_pitchfork_review(
    artist: str, album: str, review_text: str
) -> str:
    """
    Ask Claude to write a concise summary of a Pitchfork album review.

    The summary is written in Claude's own voice, drawing on the critic's
    actual review text but condensed to fit comfortably in a Discord embed.
    This is used in the monthly Best New Album post.

    Parameters
    ----------
    artist      : Artist name as scraped from Pitchfork.
    album       : Album title as scraped from Pitchfork.
    review_text : Excerpted body text from the Pitchfork review (may be
                  truncated at _REVIEW_TEXT_MAX_CHARS by the scraper).

    Returns
    -------
    str
        A 3–4 sentence summary, casual in tone and ready to embed in Discord.
    """
    prompt = f"""You are writing a short summary for a Discord music bot's monthly album review post.

Below is an excerpt from a Pitchfork review. Summarize it in 3–4 sentences in your own words,
capturing the reviewer's overall take, what makes the album distinctive, and its mood or sound.
Keep it engaging and conversational — this is for a group of music-loving friends.

Album: {album}
Artist: {artist}

Review excerpt:
{review_text}

Return only the summary text — no labels, no preamble, no quotation marks."""

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()


def summarize_criterion_film(
    title: str, director: str, year: int, overview: str
) -> str:
    """
    Ask Claude to write a short, enthusiastic pitch for a Criterion Collection
    film to post in the Discord movie night channel.

    The pitch is written in Claude's own voice, drawing on the TMDB overview.
    It should feel like a recommendation from a knowledgeable friend rather than
    a formal synopsis, and should make the group want to watch the film.

    Parameters
    ----------
    title    : Film title.
    director : Director's name.
    year     : Four-digit release year.
    overview : TMDB plot overview (may be truncated).

    Returns
    -------
    str
        A 2–3 sentence pitch, casual and enthusiastic, ready to embed in Discord.
    """
    prompt = f"""You are writing a short movie night pitch for a Discord bot.

Below is a Criterion Collection film pick. Write 2–3 sentences in your own words that make
your friends excited to watch it — mention what makes it special, its mood or style, and
why it's worth their time. Keep it casual, enthusiastic, and specific. No plot spoilers.

Film: {title} ({year})
Director: {director}

Overview:
{overview}

Return only the pitch text — no labels, no preamble, no quotation marks."""

    response = _client.messages.create(
        model=_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()
