"""Storybook generation pipeline (outline -> images -> TTS -> music -> video).

All prompts are written for a *general* storybook — no "for kids" / children framing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from google.genai import types

from app import storage
from app.config import (
    GCS_BUCKET,
    GEMINI_IMAGE_MODEL,
    GEMINI_TEXT_MODEL,
    IMAGE_GEN_CONCURRENCY,
    IMAGE_GEN_MAX_RETRIES,
    IMAGE_GEN_RETRY_DELAY_MS,
    LYRIA_MODEL,
    MEDIA_ASPECT_RATIO,
    TTS_CONCURRENCY,
    TTS_MODEL,
    TTS_VOICE,
    VEO_MODEL,
    VIDEO_GEN_CONCURRENCY,
    VIDEO_GEN_MAX_RETRIES,
    VIDEO_POLL_INTERVAL_MS,
    genai_client,
)

logger = logging.getLogger("magic-storybook.generation")

_STYLE_GUIDE = {
    "3D动画": "3D animated, Pixar-style, vibrant colors, soft lighting",
    "水彩风": "soft watercolor painting, pastel tones, gentle washes, dreamy",
    "蜡笔画": "colorful crayon drawing, playful texture, hand-drawn, warm",
    "剪纸风": "Chinese paper-cut art collage, layered paper textures, bold shapes, folk art inspired, textured edges",
    "黏土动画": "claymation stop-motion style, sculpted clay characters, tactile 3D texture, handcrafted feel, warm lighting",
    "水墨风": "traditional Chinese ink wash painting (水墨画), elegant brush strokes, black ink with subtle color accents, misty atmosphere, rice paper texture",
}

_MUSIC_STYLE_GUIDE = {
    "国风": {"genre": "Chinese traditional/folk (国风)", "instruments": "guzheng, erhu, bamboo flute (dizi), pipa, soft percussion", "mood": "elegant, flowing, culturally rich"},
    "动漫": {"genre": "anime opening/ending style", "instruments": "electric guitar, synth pads, piano, drums, orchestral strings", "mood": "energetic, adventurous, uplifting"},
    "R&B": {"genre": "smooth R&B / neo-soul", "instruments": "electric piano, smooth bass, soft drums, finger snaps, synth", "mood": "groovy, smooth, laid-back yet playful"},
    "POP": {"genre": "catchy Mandarin C-Pop", "instruments": "piano, acoustic guitar, light drums, claps, synth hooks", "mood": "catchy, bright, fun, radio-friendly"},
    "儿歌": {"genre": "sweet acoustic sing-along", "instruments": "xylophone, piano, ukulele, gentle percussion", "mood": "sweet, warm, whimsical"},
    "RAP": {"genre": "Mandarin rap / hip-hop", "instruments": "boom-bap beats, bass, hi-hats, claps, playful synth stabs", "mood": "bouncy, rhythmic, fun, confident"},
    "摇篮曲": {"genre": "lullaby / calm bedtime song", "instruments": "music box, soft piano, gentle strings, harp, celesta", "mood": "soothing, tender, dreamy, calming"},
    "古典": {"genre": "light classical / orchestral", "instruments": "strings quartet, piano, flute, harp, light woodwinds", "mood": "graceful, elegant, fairy-tale-like"},
    "电子": {"genre": "electronic / EDM lite", "instruments": "synth leads, arpeggiated pads, electronic drums, chiptune accents", "mood": "futuristic, playful, bouncy, colorful"},
}


# ── Outline ─────────────────────────────────────────────────────────────────
async def generate_outline(
    title: str, theme: str, style: str, page_count: int, user_outline: str | None
) -> dict:
    outline_ref = (
        f"- 用户提供的大纲参考：{user_outline}" if user_outline else ""
    )
    if style:
        style_line = f"- 画面风格：{style}（{_STYLE_GUIDE.get(style, style)}）"
        img_style_snippet = f"{_STYLE_GUIDE.get(style, style)} style, "
    else:
        style_line = (
            "- 画面风格：未指定——请你根据书名和主题，选择最贴切的一种画面风格"
            "（可用常用：3D动画/水彩风/蜡笔画/剪纸风/黏土动画/水墨风，或自定义），"
            "填入下方 JSON 的 style 字段，并在所有 imagePrompt 中一致使用这一风格"
        )
        img_style_snippet = ""
    prompt = f"""你是一位顶级的绘本创作专家。请根据书名与主题，创作一个完整的故事大纲和详细分镜脚本。

绘本信息：
- 书名：{title}
- 主题：{theme}
{style_line}
- 页数：{page_count}页
{outline_ref}

请严格按照以下JSON格式返回（只返回JSON，不要有任何额外文字）：
{{
  "title": "书名",
  "theme": "主题",
  "style": "最终采用的画面风格（中文简称，如 3D动画/水彩风/... 或自定义；若上方已指定则填该值）",
  "musicStyle": "最适合本绘本的主题曲风格（如 国风/动漫/R&B/POP/儿歌/RAP/摇篮曲/古典/电子，或自定义）",
  "summary": "故事简介（2-3句话，充满想象力）",
  "characters": [
    {{
      "name": "角色名字",
      "description": "Detailed English description for image consistency: approximate age, hairstyle, clothing, distinguishing features (e.g. round glasses, a yellow backpack). Be very specific so every illustration keeps this character looking the same."
    }}
  ],
  "pages": [
    {{
      "pageNum": 1,
      "title": "这一页的小标题（5字以内）",
      "text": "这一页的故事文字（1句话，简短有力，生动有趣）",
      "imagePrompt": "Detailed English image generation prompt: [scene description], {img_style_snippet}storybook illustration, rich colors, expressive characters, high quality, soft lighting, no text in image",
      "interactiveHint": "可选的一句话看点或引导（10字以内，需与主题契合；不适合则留空）",
      "ttsPrompt": "本页的TTS朗读提示词（中文），用于语音合成。根据本页情节和情绪，加入丰富的英文表达标签让朗读更生动有感情。\\n可用情绪标签举例：[excitement] [curiosity] [awe] [enthusiasm] [hope] [determination] [nervousness] [tension] [confusion] [positive] [neutral] [negative]\\n语速与节奏标签：[slow] [fast] [short pause] [long pause]\\n非语言发声标签：[laughs] [whispers] [gasps]\\n示例：'[enthusiasm] 哇！[short pause] 快看快看！[curiosity] 那是什么呀？[long pause] [whispers] 是一颗会发光的小星星呢！[awe] 好漂亮啊！[laughs]'"
    }}
  ]
}}

创作要求：
1. **根据书名和主题自行判断合适的受众、题材与语气，不要默认面向儿童**。故事的用词、复杂度与情感基调都要贴合主题——可以是温馨、悬疑、浪漫、励志、幽默、恐怖、科幻、史诗、文艺等任何合适的风格
2. **根据主题为绘本挑选最合适的画面风格(style)与主题曲风格(musicStyle)**；若上方已指定画面风格则 style 用指定值；所有 imagePrompt 使用统一的画面风格
3. 故事有完整的起承转合，节奏张弛有度，有记忆点与情感共鸣
4. 每页文字简短凝练（以1句话为主），与画面相辅相成，语言风格与主题一致
5. 图片提示词要极其详细，注意图片中不要包含任何文字
6. interactiveHint 为可选项：仅在与主题契合时给出一句话看点/引导，否则留空
7. ttsPrompt 是给语音合成引擎的朗读脚本，要比 text 更丰富、更有表现力，情绪基调与主题一致。朗读文字必须是中文，但所有表达标签必须使用英文（如 [excitement]、[whispers]、[slow]），不要翻译标签
8. **角色一致性极其重要**：characters 数组必须为每个角色提供非常详细的英文外观描述（年龄、发型、发色、服装颜色和款式、配饰、体型等），确保每页插画中的角色外观完全一致。每页的 imagePrompt 中必须引用 characters 中的描述来保持角色视觉连续性"""

    response = await genai_client.aio.models.generate_content(
        model=GEMINI_TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.95, max_output_tokens=8192),
    )
    raw = response.text or ""
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("故事大纲返回格式无效")
    return json.loads(match.group(0))


# ── Page image ──────────────────────────────────────────────────────────────
_IMG_STYLE = {
    "3D动画": "3D animated Pixar-style",
    "水彩风": "soft watercolor painting",
    "蜡笔画": "colorful crayon drawing",
    "剪纸风": "Chinese paper-cut art collage, layered paper textures, bold shapes",
    "黏土动画": "claymation stop-motion style, sculpted clay characters, tactile 3D texture",
    "水墨风": "traditional Chinese ink wash painting, elegant brush strokes, misty atmosphere",
}


async def generate_page_image(
    image_prompt: str, style: str, character_descs: str = ""
) -> tuple[bytes, str]:
    style_desc = _IMG_STYLE.get(style, style)
    char_section = (
        f"\nCharacter reference (MUST follow exactly for consistency across all pages):\n{character_descs}\n"
        if character_descs
        else ""
    )
    full_prompt = f"""Create a storybook page illustration.

Scene: {image_prompt}
{char_section}
Style requirements:
- {style_desc} art style
- High quality; colors, mood and composition should match the story's tone and intended audience (do not default to a childish look)
- Characters MUST match the character reference descriptions exactly (same hairstyle, clothing, accessories) for visual consistency across all pages
- DO NOT include any text, letters, words, or characters in the image
- Pure illustration only, no text overlay
- Beautiful composition with vibrant colors and soft lighting"""

    response = await genai_client.aio.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            temperature=1.0,
            image_config=types.ImageConfig(aspect_ratio=MEDIA_ASPECT_RATIO),
        ),
    )
    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            return part.inline_data.data, part.inline_data.mime_type
    raise ValueError("图片生成响应中没有图片数据")


async def generate_page_image_with_retry(
    image_prompt: str, style: str, label: str = "", character_descs: str = ""
) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(1, IMAGE_GEN_MAX_RETRIES + 1):
        try:
            return await generate_page_image(image_prompt, style, character_descs)
        except Exception as err:  # noqa: BLE001
            last_error = err
            logger.warning(
                "%s 图片生成失败 (第 %d/%d 次): %s",
                label, attempt, IMAGE_GEN_MAX_RETRIES, err,
            )
            if attempt < IMAGE_GEN_MAX_RETRIES:
                await asyncio.sleep(IMAGE_GEN_RETRY_DELAY_MS * attempt / 1000)
    raise last_error  # type: ignore[misc]


# ── Page narration (TTS) ────────────────────────────────────────────────────
_TTS_SYSTEM_PROMPT = """## SCENE:
A warm, intimate recording booth with soft acoustic treatment. The microphone
captures a close, personal storytelling feel with gentle proximity warmth.

## STYLE:
* Expressive Narrator: Speak as an engaging narrator reading a story aloud.
  Match the delivery to the story's genre and mood (tender, suspenseful,
  dramatic, humorous, epic, etc.); do not default to a childish sing-song tone.
* Dynamics: Vary energy to match the story — soft and gentle for tender moments,
  bright and excited for surprises, slow and mysterious for suspense.
* Pacing: Use natural pauses to let dramatic moments land. Slow down for
  important or emotional parts, speed up slightly for exciting action.
* Character Voices: When dialogue appears, give subtle vocal shifts to
  differentiate characters while keeping the overall warm narrator tone.
* Non-verbal Texture: Use natural laughs, gasps, and whispers where tagged to
  make the narration feel alive and engaging.
* Language: The speech content is in Chinese (Mandarin). All expression tags
  (e.g. [excitement], [curiosity], [whispers], [slow]) are in English and must
  be kept as-is — do NOT translate them."""


async def generate_page_audio(
    tts_prompt: str, title: str, text: str, hint: str
) -> bytes:
    transcript = tts_prompt or f"{title}。{text}{('。' + hint) if hint else ''}"
    response = await genai_client.aio.models.generate_content(
        model=TTS_MODEL,
        contents=[
            types.Content(
                role="user",
                parts=[types.Part(text=f"{_TTS_SYSTEM_PROMPT}\n\n## TRANSCRIPT\n{transcript}")],
            )
        ],
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                language_code="cmn-CN",
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=TTS_VOICE)
                ),
            ),
        ),
    )
    data = response.candidates[0].content.parts[0].inline_data.data
    if not data:
        raise ValueError("TTS 返回无音频数据")
    return storage.pcm_to_wav(data)


# ── Theme music (Lyria) ─────────────────────────────────────────────────────
def _lyria_to_lrc(raw_lyrics: str) -> str:
    meta = re.search(r"mosic:\s*[\d.]", raw_lyrics)
    lyrics_only = raw_lyrics[: meta.start()].strip() if meta else raw_lyrics.strip()
    stripped = re.sub(r"\[\[[A-Z]\d+\]\]", "", lyrics_only)

    ts_positions = [
        (float(m.group(1)), m.start(), m.end())
        for m in re.finditer(r"\[(\d+\.?\d*):\]", stripped)
    ]
    segments = []
    for i, (sec, _start, end) in enumerate(ts_positions):
        text_end = ts_positions[i + 1][1] if i + 1 < len(ts_positions) else len(stripped)
        text = stripped[end:text_end].strip()
        if not text:
            continue
        lines = [ln.strip() for ln in re.split(r"\[:\]\s*", text) if ln.strip()]
        next_sec = ts_positions[i + 1][0] if i + 1 < len(ts_positions) else sec + len(lines) * 4
        segments.append((sec, next_sec, lines))

    def _fmt(s: float) -> str:
        m = int(s // 60)
        return f"{m:02d}:{s - m * 60:05.2f}"

    lrc_lines = []
    for start_sec, next_sec, lines in segments:
        interval = (next_sec - start_sec) / len(lines) if len(lines) > 1 else (next_sec - start_sec)
        for idx, line in enumerate(lines):
            lrc_lines.append(f"[{_fmt(start_sec + idx * interval)}] {line}")
    return "\n".join(lrc_lines)


async def generate_book_music(
    title: str, summary: str, theme: str, page_texts: list[str], music_style: str
) -> tuple[bytes, str]:
    # Custom (non-reference) music styles: use the raw label as the genre so
    # the user's choice actually shapes the song, instead of falling back.
    style_info = _MUSIC_STYLE_GUIDE.get(music_style) or {
        "genre": music_style,
        "instruments": "instrumentation fitting the style",
        "mood": "mood fitting the style",
    }
    story_context = "。".join(page_texts)
    prompt = f"""Create a Mandarin Chinese song in {style_info['genre']} style for a storybook called "{title}".
Theme: {theme}. Story summary: {summary}.
Story content: {story_context}

The song MUST have Chinese (Mandarin) lyrics that tell and retell the story — the lyrics should directly reference the story's characters, scenes, and plot.

Music style requirements:
- Genre: {style_info['genre']}
- Mood: {style_info['mood']}
- Instrumentation: {style_info['instruments']}
- Sung in Chinese (Mandarin) with simple, catchy lyrics about the story
- About 1-2 minutes long
- Make the melody memorable"""

    response = await genai_client.aio.models.generate_content(
        model=LYRIA_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["AUDIO", "TEXT"]),
    )
    audio_data: bytes | None = None
    raw_lyrics = ""
    for part in response.candidates[0].content.parts:
        if part.text and not raw_lyrics:
            raw_lyrics = part.text
        elif part.inline_data and part.inline_data.data:
            audio_data = part.inline_data.data
    if not audio_data:
        raise ValueError("Lyria 音乐生成返回无音频数据")
    return audio_data, _lyria_to_lrc(raw_lyrics)


# ── Page video (Veo) ────────────────────────────────────────────────────────
async def generate_page_video(
    image_gcs_uri: str, image_mime: str, story_text: str, output_gcs_uri: str, label: str = ""
) -> str:
    prompt = (
        f"Gently animate this illustration with subtle, magical movement. Story context: {story_text}. "
        "Add soft dreamy motion to the scene, gentle character animation, warm lighting. "
        "Include a soft gentle narration of the story and light ambient sound that matches the scene. "
        "Keep original art style consistent. No text overlay."
    )
    operation = await genai_client.aio.models.generate_videos(
        model=VEO_MODEL,
        prompt=prompt,
        image=types.Image(gcs_uri=image_gcs_uri, mime_type=image_mime),
        config=types.GenerateVideosConfig(
            aspect_ratio=MEDIA_ASPECT_RATIO,  # match the page illustration ratio
            person_generation="allow_all",
            generate_audio=True,  # Veo generates audio (音画同出)
            output_gcs_uri=output_gcs_uri,
        ),
    )
    while not operation.done:
        await asyncio.sleep(VIDEO_POLL_INTERVAL_MS / 1000)
        operation = await genai_client.aio.operations.get(operation)
    if operation.error:
        raise ValueError(f"Veo operation error: {operation.error}")
    videos = getattr(operation.response, "generated_videos", None)
    if videos and videos[0].video:
        logger.info("%s video generated: %s", label, videos[0].video.uri)
        return videos[0].video.uri
    raise ValueError("Veo video generation returned no video data")


async def generate_page_video_with_retry(
    image_gcs_uri: str, image_mime: str, story_text: str, output_gcs_uri: str, label: str = ""
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, VIDEO_GEN_MAX_RETRIES + 1):
        try:
            return await generate_page_video(
                image_gcs_uri, image_mime, story_text, output_gcs_uri, label
            )
        except Exception as err:  # noqa: BLE001
            last_error = err
            logger.warning("%s 视频生成失败 (第 %d/%d 次): %s", label, attempt, VIDEO_GEN_MAX_RETRIES, err)
            if re.search(r"support codes?:", str(err), re.I):
                break
            if attempt < VIDEO_GEN_MAX_RETRIES:
                await asyncio.sleep(5 * attempt)
    raise last_error  # type: ignore[misc]


async def _bounded_gather(coros, concurrency: int):
    sem = asyncio.Semaphore(concurrency)

    async def _run(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*[_run(c) for c in coros], return_exceptions=True)


# ── Background video job ─────────────────────────────────────────────────────
async def run_video_generation_job(book_id: str, pages: list[dict]) -> None:
    actual = len(pages)
    logger.info("[Book %s] 🎬 Starting video generation for %d pages", book_id, actual)
    try:
        await storage.save_book(book_id, {"videoStatus": "generating", "videoProgress": 0})
        generated: dict[int, str] = {}
        completed = 0

        async def _one(i: int) -> None:
            nonlocal completed
            page = pages[i]
            if not page or not page.get("imagePath"):
                return
            gcs_uri = storage.path_to_gcs_uri(page["imagePath"])
            mime = storage.mime_type_from_path(page["imagePath"])
            page_num = str(i + 1).zfill(2)
            out_uri = f"gs://{GCS_BUCKET}/books/{book_id}/video_p{page_num}/"
            try:
                video_uri = await generate_page_video_with_retry(
                    gcs_uri, mime, page.get("text", ""), out_uri, f"[Book {book_id}] Video P{i + 1}"
                )
                generated[i] = storage.gcs_uri_to_path(video_uri)
                completed += 1
            except Exception as err:  # noqa: BLE001
                logger.error("[Book %s] Video P%d failed: %s", book_id, i + 1, err)

        for start in range(0, actual, VIDEO_GEN_CONCURRENCY):
            batch = range(start, min(start + VIDEO_GEN_CONCURRENCY, actual))
            await _bounded_gather([_one(i) for i in batch], VIDEO_GEN_CONCURRENCY)
            fresh = await storage.get_book(book_id)
            if fresh and fresh.get("pages"):
                for idx, vpath in generated.items():
                    if idx < len(fresh["pages"]):
                        fresh["pages"][idx]["videoPath"] = vpath
                await storage.save_book(book_id, {"pages": fresh["pages"], "videoProgress": completed})

        final = await storage.get_book(book_id)
        pages_out = final.get("pages") if final else None
        if pages_out:
            for idx, vpath in generated.items():
                if idx < len(pages_out):
                    pages_out[idx]["videoPath"] = vpath
        await storage.save_book(
            book_id,
            {
                **({"pages": pages_out} if pages_out else {}),
                "videoStatus": "complete",
                "videoProgress": completed,
                "videoCompletedAt": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("[Book %s] 🎬 ✅ All videos complete (%d/%d)", book_id, completed, actual)
    except Exception as err:  # noqa: BLE001
        logger.error("[Book %s] 🎬 ❌ Video generation failed: %s", book_id, err)
        await storage.save_book(book_id, {"videoStatus": "error"})


# ── Main generation job (fire-and-forget) ────────────────────────────────────
async def run_generation_job(
    book_id: str,
    title: str,
    theme: str,
    style: str = "",
    page_count: int = 6,
    outline: str | None = None,
    music_style: str = "",
) -> None:
    try:
        await storage.save_book(book_id, {"status": "generating_outline", "progress": 0, "statusMessage": "正在构思故事大纲..."})
        story = await generate_outline(title, theme, style, page_count, outline)
        actual = min(len(story["pages"]), page_count)

        # If画风/主题曲 weren't specified, use what the model chose from the theme.
        style = style or story.get("style") or "3D动画"
        music_style = music_style or story.get("musicStyle") or "儿歌"

        await storage.save_book(book_id, {
            "status": "generating_images",
            "style": style,
            "musicStyle": music_style,
            "summary": story.get("summary", ""),
            "totalPages": actual,
            "progress": 1,
            "statusMessage": f"大纲完成！开始生成 {actual} 页插画和主题曲...",
        })

        page_texts = [p["text"] for p in story["pages"][:actual]]

        characters = story.get("characters", [])
        character_descs = "\n".join(f"- {c['name']}: {c['description']}" for c in characters)
        if characters:
            await storage.save_book(book_id, {"characters": characters})

        # Pre-populate page records from the outline so illustrations (imagePath)
        # and narration (audioPath) can be filled in CONCURRENTLY — TTS only needs
        # the page text, so it no longer waits for the images. Firestore stores
        # object PATHS; signed URLs are minted on read.
        pages: list[dict] = [
            {
                "pageNum": pd.get("pageNum"),
                "title": pd.get("title"),
                "text": pd.get("text"),
                "imagePrompt": pd.get("imagePrompt"),
                "interactiveHint": pd.get("interactiveHint", ""),
                "ttsPrompt": pd.get("ttsPrompt", ""),
                "imagePath": "",
            }
            for pd in story["pages"][:actual]
        ]

        # ── Kick off music + TTS in parallel with image generation ──
        async def _music() -> str | None:
            try:
                audio, lyrics = await generate_book_music(title, story.get("summary", theme), theme, page_texts, music_style)
                path = await storage.upload_bytes(audio, f"books/{book_id}/theme_music.mp3", "audio/mpeg")
                await storage.save_book(book_id, {"musicPath": path, "musicLyrics": lyrics})
                return path
            except Exception as err:  # noqa: BLE001
                logger.warning("[Book %s] 🎵 music failed: %s", book_id, err)
                return None

        async def _tts(i: int) -> None:
            p = pages[i]
            try:
                wav = await generate_page_audio(p.get("ttsPrompt", ""), p.get("title", ""), p.get("text", ""), p.get("interactiveHint", ""))
                p["audioPath"] = await storage.upload_bytes(wav, f"books/{book_id}/audio_p{str(i + 1).zfill(2)}.wav", "audio/wav")
            except Exception as err:  # noqa: BLE001
                logger.warning("[Book %s] TTS P%d failed: %s", book_id, i + 1, err)

        async def _all_tts() -> None:
            for start in range(0, actual, TTS_CONCURRENCY):
                batch = range(start, min(start + TTS_CONCURRENCY, actual))
                await _bounded_gather([_tts(i) for i in batch], TTS_CONCURRENCY)

        music_task = asyncio.create_task(_music())
        tts_task = asyncio.create_task(_all_tts())

        # ── Images (batched), running concurrently with music + TTS ──
        async def _image(i: int) -> None:
            pd = story["pages"][i]
            try:
                data, mime = await generate_page_image_with_retry(
                    pd["imagePrompt"], style, f"[Book {book_id}] Page {i + 1}", character_descs
                )
                ext = mime.split("/")[1] or "png"
                pages[i]["imagePath"] = await storage.upload_bytes(
                    data, f"books/{book_id}/page_{str(i + 1).zfill(2)}.{ext}", mime
                )
            except Exception as err:  # noqa: BLE001
                logger.error("[Book %s] Page %d image failed: %s", book_id, i + 1, err)

        for start in range(0, actual, IMAGE_GEN_CONCURRENCY):
            batch = range(start, min(start + IMAGE_GEN_CONCURRENCY, actual))
            await storage.save_book(book_id, {"progress": start + 1, "statusMessage": f"正生成第 {start + 1}-{batch.stop}/{actual} 页插画（配音、主题曲同步进行）..."})
            await _bounded_gather([_image(i) for i in batch], IMAGE_GEN_CONCURRENCY)
            await storage.save_book(book_id, {"pages": pages, "progress": batch.stop + 1, "statusMessage": f"已完成 {batch.stop}/{actual} 页插画..."})

        # Wait for the parallel narration + theme song to finish.
        await storage.save_book(book_id, {"statusMessage": "正在完成配音与主题曲..."})
        await tts_task
        music_path = await music_task
        failed = sum(1 for p in pages if not p.get("imagePath"))

        complete_data = {
            "status": "complete",
            "pages": pages,
            "pageCount": actual,
            "failedPages": failed,
            "progress": actual + 2,
            "statusMessage": (f"绘本创作完成！（{failed} 页插画生成失败，可通过编辑重新生成）" if failed else "绘本创作完成！"),
            "completedAt": datetime.now(timezone.utc).isoformat(),
        }
        if music_path:
            complete_data["musicPath"] = music_path
        await storage.save_book(book_id, complete_data)
        logger.info("[Book %s] ✅ Generation complete: '%s' (%d pages, %d failed)", book_id, title, actual, failed)

        # Fire-and-forget video generation
        asyncio.create_task(run_video_generation_job(book_id, pages))
    except Exception as err:  # noqa: BLE001
        logger.error("[Book %s] ❌ Generation failed: %s", book_id, err)
        await storage.save_book(book_id, {"status": "error", "statusMessage": str(err)})
