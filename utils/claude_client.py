"""
utils/claude_client.py
──────────────────────
Wrapper around the Anthropic Python SDK.

All Claude API calls in the project go through this module so that
model selection, error handling, and JSON parsing live in one place.

Public functions
----------------
generate_trivia_question(sports)  → dict
    Generate a multiple-choice sports trivia question.

describe_release(artist, title, genre)  → str
    Write a short hype blurb for a Spotify release to include in the
    weekly music post.

summarize_pitchfork_review(artist, album, review_text)  → str
    Summarize a Pitchfork album review into 3–4 sentences for the
    monthly album review embed.
"""

import json
import logging
import re

import anthropic

import config

logger = logging.getLogger(__name__)

# Instantiate the client once at module load. The Anthropic client is
# stateless and thread-safe, so a single instance is fine for the whole bot.
_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# claude-haiku is the fastest and cheapest Claude model — well suited for
# these short-output tasks where latency matters more than deep reasoning.
_MODEL = "claude-haiku-4-5-20251001"


def generate_trivia_question(sports: list[str]) -> dict:
    """
    Ask Claude to generate one multiple-choice sports trivia question.

    The prompt instructs Claude to return ONLY a JSON object so we can parse
    it reliably without further text extraction.

    Parameters
    ----------
    sports : list[str]
        Sport names Claude can draw from, e.g. ["soccer", "baseball"].

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

    prompt = f"""You are a sports trivia expert writing questions for a group of dedicated, knowledgeable fans.

Generate one hard multiple-choice trivia question about one of these sports: {sports_str}.

The audience follows these sports closely, so avoid anything a casual fan would know. Good question topics include:
- Specific records, statistics, or milestones (e.g. exact numbers, career totals, single-season marks)
- Obscure historical facts (e.g. rule changes, founding moments, defunct teams)
- Draft history, trades, and roster moves that serious followers would recall
- Awards and honours beyond the most famous ones
- Coaches, referees, or front-office figures rather than just star players
- Unusual or lesser-known moments in playoff/championship history

Avoid: championship winners of recent major tournaments, MVP winners of the last decade, currently active superstars' well-known achievements, or any question a casual viewer could guess.

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
