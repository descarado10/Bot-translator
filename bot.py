import pickle
from pathlib import Path
import logging
import asyncio
import os
import html
from difflib import SequenceMatcher
from deepmultilingualpunctuation import PunctuationModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import speech_recognition as sr
from deep_translator import GoogleTranslator, MyMemoryTranslator, YandexTranslator
import easyocr
from pydub import AudioSegment

# --- KONSTANTALAR VA SOZLAMALAR ---
BOT_TOKEN = "8154424171:AAGyXlTUQK1fKuenDfluts_d1yhlBQVurDI"
USER_STATES_FILE = "user_states.pkl"
MAIN_MENU_BUTTONS = ["✍️ Matn Tarjimasi", "🎙️ Ovoz Tarjimasi", "🎬 Video Tarjimasi", "🖼️ Rasmdan Tarjima"]
DIRECTIONS_MAP = {
    "🇺🇿 UZ-RU 🇷🇺": ("uz", "ru"), "🇷🇺 RU-UZ 🇺🇿": ("ru", "uz"),
    "🇺🇿 UZ-EN 🇬🇧": ("uz", "en"), "🇬🇧 EN-UZ 🇺🇿": ("en", "uz"),
    "🇷🇺 RU-EN 🇬🇧": ("ru", "en"), "🇬🇧 EN-RU 🇷🇺": ("en", "ru"),
}
TRANSLATOR_FALLBACK_CHAIN = [
    ("Yandex", YandexTranslator),
    ("Google", GoogleTranslator),
    ("MyMemory", MyMemoryTranslator)
]

# --- GLOBAL O'ZGARUVCHILAR ---
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
punct_model = PunctuationModel()
ocr_reader_latin = None
ocr_reader_cyrillic = None
user_states = {}

# --- YORDAMCHI FUNKSIYALAR ---
def is_similar(a, b, threshold=0.92):
    return SequenceMatcher(None, a, b).ratio() > threshold

def smart_punctuate(text: str) -> str:
    try:
        return punct_model.restore_punctuation(text)
    except Exception as e:
        logging.warning(f"Punktuatsiya xatosi: {e}")
        return text

def save_user_states():
    """Foydalanuvchi holatlarini saqlash"""
    try:
        with open(USER_STATES_FILE, "wb") as f:
            pickle.dump(user_states, f)
    except Exception as e:
        logging.error(f"Holatlarni saqlashda xato: {e}")

def load_user_states():
    """Saqlangan foydalanuvchi holatlarini yuklash"""
    if Path(USER_STATES_FILE).exists():
        try:
            with open(USER_STATES_FILE, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logging.error(f"Holatlarni yuklashda xato: {e}")
            return {}
    return {}

def get_main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    for text in MAIN_MENU_BUTTONS: builder.add(types.KeyboardButton(text=text))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_directions_keyboard():
    builder = ReplyKeyboardBuilder()
    for text in DIRECTIONS_MAP.keys(): builder.add(types.KeyboardButton(text=text))
    builder.adjust(2)
    builder.row(types.KeyboardButton(text="⬅️ Bosh menyu"))
    return builder.as_markup(resize_keyboard=True)

def split_text(text, max_words=25):
    words = text.split()
    chunks = [" ".join(words[i:i+max_words]) for i in range(0, len(words), max_words)]
    return chunks

async def get_reliable_translation(text, source_lang, target_lang):
    if not text:
        return None, None
    chunks = split_text(text)
    translated_chunks = []
    translator_name = "Noma'lum"
    for i, chunk in enumerate(chunks):
        translated_chunk = None
        for t_name, translator_class in TRANSLATOR_FALLBACK_CHAIN:
            try:
                loop = asyncio.get_running_loop()
                translated_part = await loop.run_in_executor(
                    None, lambda: translator_class(source=source_lang, target=target_lang).translate(chunk)
                )
                if translated_part and translated_part.strip():
                    translated_chunk = translated_part.strip()
                    translator_name = t_name
                    logging.info(f"[{i}] ({t_name}) Tarjima: {translated_chunk}")
                    break
            except Exception as e:
                logging.warning(f"[{i}] {t_name} xato: {e}")
        if translated_chunk:
            translated_chunks.append(translated_chunk)
        else:
            logging.warning(f"[{i}] Tarjima xatosi: {chunk[:30]}...")
            translated_chunks.append("[Tarjima xatosi]")
    if not translated_chunks:
        return None, None
    return " ".join(translated_chunks), translator_name

async def audio_to_text(file_path: str, source_language_code: str):
    wav_filename = f"{file_path}.wav"
    try:
        audio = AudioSegment.from_file(file_path)
        audio.export(wav_filename, format="wav")
    except Exception as e:
        logging.error(f"Could not convert/extract audio: {e}")
        return None
    recognizer = sr.Recognizer()
    recognized_text = None
    with sr.AudioFile(wav_filename) as source:
        audio_data = recognizer.record(source)
    try:
        lang_map = {'uz': 'uz-UZ', 'ru': 'ru-RU', 'en': 'en-US'}
        google_lang_code = lang_map.get(source_language_code, 'en-US')
        result = recognizer.recognize_google(audio_data, language=google_lang_code, show_all=True)
        if isinstance(result, dict) and "alternative" in result:
            transcripts = []
            for alt in result["alternative"]:
                text = alt.get("transcript")
                if text:
                    text = text.strip()
                    # O'xshash matnlarni filtrlash
                    if not any(is_similar(text, t) for t in transcripts):
                        transcripts.append(text)
            recognized_text = ". ".join(transcripts)
        else:
            recognized_text = recognizer.recognize_google(audio_data, language=google_lang_code)
        if recognized_text:
            recognized_text = smart_punctuate(recognized_text)
    except Exception as e:
        logging.warning(f"Speech Recognition failed: {e}")
    finally:
        if os.path.exists(wav_filename): 
            os.remove(wav_filename)
    return recognized_text

async def image_to_text(file_path: str) -> str | None:
    if not ocr_reader_latin or not ocr_reader_cyrillic:
        logging.error("OCR Reader yuklanmagan.")
        return "OCR xizmati ishlamayapti."
    try:
        result_latin = ocr_reader_latin.readtext(file_path, detail=0, paragraph=True)
        result_cyrillic = ocr_reader_cyrillic.readtext(file_path, detail=0, paragraph=True)
        full_text = []
        if result_latin: full_text.extend(result_latin)
        if result_cyrillic: full_text.extend(result_cyrillic)
        if not full_text: return None
        return " ".join(full_text)
    except Exception as e:
        logging.error(f"EasyOCR error: {e}")
        return None

async def process_translation_request(message: types.Message, text_to_translate: str, status_message: types.Message = None):
    """Umumiy tarjima mantig'i."""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or 'direction' not in state: 
        return
    
    if status_message:
        await status_message.edit_text("⏳ Matn tarjima qilinmoqda...")
    else:
        status_message = await message.answer("⏳ Matn tarjima qilinmoqda...")
        
    source_lang, target_lang = state['direction']
    translated_text, translator_name = await get_reliable_translation(text_to_translate, source_lang, target_lang)
    
    if status_message:
        await status_message.delete()
    
    if translated_text:
        response = f"<b>Tarjima ({translator_name}):</b>\n\n{html.escape(translated_text)}"
    else:
        response = "😔 Kechirasiz, tarjima qilishda xatolik yuz berdi."
    
    await message.answer(response, reply_markup=get_main_menu_keyboard())
    if user_id in user_states:
        del user_states[user_id]
        save_user_states()

async def process_media(message: types.Message, file_id: str, file_ext: str, is_photo: bool = False):
    """Ovoz, video yoki rasmni qayta ishlaydi va statusni ko'rsatib boradi."""
    user_id = message.from_user.id
    if user_id not in user_states or 'direction' not in user_states[user_id]:
        await message.answer("Iltimos, avval tarjima yo'nalishini tanlang.", reply_markup=get_main_menu_keyboard())
        return

    status_msg = await message.answer(f"✅ Fayl qabul qilindi. Yuklab olinmoqda...")
    
    file = await message.bot.get_file(file_id)
    os.makedirs("downloads", exist_ok=True)
    input_filename = f"downloads/{file_id}.{file_ext}"
    await message.bot.download_file(file.file_path, destination=input_filename)

    if is_photo:
        await status_msg.edit_text("🖼️ Rasmdagi matn aniqlanmoqda...")
        recognized_text = await image_to_text(input_filename)
    else:
        await status_msg.edit_text("🎵 Ovoz matnga o'girilmoqda...")
        recognized_text = await audio_to_text(input_filename, user_states[user_id]['direction'][0])
    
    if os.path.exists(input_filename):
        os.remove(input_filename)

    if recognized_text:
        safe_recognized_text = html.escape(recognized_text)
        await message.answer(f"<b>Aniqlangan matn:</b>\n<i>{safe_recognized_text}</i>")
        await process_translation_request(message, recognized_text, status_msg)
    else:
        await status_msg.edit_text("❌ Matnni aniqlab bo'lmadi.")
        await asyncio.sleep(3)
        await status_msg.delete()
        if user_id in user_states:
            del user_states[user_id]
            save_user_states()
        await message.answer("Bosh menyu", reply_markup=get_main_menu_keyboard())

# --- BOT QAYTA ISHGA TUSHGANDA FOYDALANUVCHILARGA XABAR YUBORISH ---
async def notify_users_on_startup():
    """Bot qayta ishga tushganda kutib qolgan so'rovlarni tekshirish"""
    if not user_states:
        return

    failed_users = []
    for user_id, state in user_states.items():
        try:
            mode = state.get('mode')
            direction = state.get('direction')
            
            if mode and direction:
                message_map = {
                    'text': "✍️ Matn tarjimasi",
                    'voice': "🎙️ Ovoz tarjimasi",
                    'video': "🎬 Video tarjimasi",
                    'photo': "🖼️ Rasm tarjimasi"
                }
                
                src, tgt = direction
                direction_text = f"{src.upper()} ➡️ {tgt.upper()}"
                
                await bot.send_message(
                    chat_id=user_id,
                    text=f"🤖 Bot qayta ishga tushdi!\n\n"
                         f"🔹 Sizning oxirgi so'rovingiz: {message_map[mode]}\n"
                         f"🔹 Yo'nalish: {direction_text}\n\n"
                         "Iltimos, so'rovingizni davom ettiring yoki yangisini boshlang.",
                    reply_markup=get_main_menu_keyboard()
                )
        except Exception as e:
            logging.error(f"Foydalanuvchi {user_id} ga xabar yuborishda xato: {e}")
            failed_users.append(user_id)
    
    # Muvaffaqiyatsiz urinishlar uchun holatlarni tozalash
    for user_id in failed_users:
        if user_id in user_states:
            del user_states[user_id]
    save_user_states()

# --- TELEGRAM HANDLER'LARI ---
@dp.message(CommandStart())
@dp.message(F.text == "⬅️ Bosh menyu")
@dp.message(F.text == "🏠 Bosh menyu")
async def handle_start_and_back(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_states:
        del user_states[user_id]
        save_user_states()
    await message.answer("Assalomu alaykum! Kerakli bo'limni tanlang:", reply_markup=get_main_menu_keyboard())

@dp.message(F.text.in_(MAIN_MENU_BUTTONS))
async def handle_main_menu_selection(message: types.Message):
    user_id = message.from_user.id
    mode_text = message.text
    mode = None
    if "Matn" in mode_text: mode = 'text'
    elif "Ovoz" in mode_text: mode = 'voice'
    elif "Video" in mode_text: mode = 'video'
    elif "Rasmdan" in mode_text: mode = 'photo'
    user_states[user_id] = {'mode': mode}
    save_user_states()
    await message.answer("Endi tarjima yo'nalishini tanlang:", reply_markup=get_directions_keyboard())

@dp.message(F.text.in_(DIRECTIONS_MAP.keys()))
async def handle_direction_selection(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or 'mode' not in state:
        await handle_start_and_back(message)
        return
    state['direction'] = DIRECTIONS_MAP[message.text]
    save_user_states()
    mode = state['mode']
    
    # Tugmalar panelini yaratish
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="⬅️ Orqaga"))
    builder.add(types.KeyboardButton(text="🏠 Bosh menyu"))
    builder.adjust(2)
    
    instruction_map = {
        'text': "Tarjima uchun matn yuboring:",
        'voice': "Tarjima uchun ovozli xabar yuboring:",
        'video': "Tarjima uchun video (20 MB gacha) yuboring:",
        'photo': "Tarjima uchun rasm yuboring:"
    }
    
    await message.answer(
        instruction_map.get(mode, ""),
        reply_markup=builder.as_markup(resize_keyboard=True)
    )

@dp.message(F.text == "⬅️ Orqaga")
async def handle_back_button(message: types.Message):
    # Yo'nalishlarni qayta ko'rsatish
    await message.answer(
        "Tarjima yo'nalishini tanlang:",
        reply_markup=get_directions_keyboard()
    )

@dp.message(F.text)
async def handle_text_input(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get('mode') != 'text' or 'direction' not in state:
        await message.answer("Noto'g'ri buyruq. Iltimos, bosh menyudan kerakli bo'limni tanlang.", reply_markup=get_main_menu_keyboard())
        return
    await process_translation_request(message, message.text)

@dp.message(F.photo)
async def handle_photo_input(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get('mode') != 'photo' or 'direction' not in state:
        await message.answer("Rasm tarjimasi uchun avval menyudan '🖼️ Rasmdan Tarjima' bo'limini tanlang.", reply_markup=get_main_menu_keyboard())
        return
    file_id = message.photo[-1].file_id
    await process_media(message, file_id, "jpg", is_photo=True)

@dp.message(F.voice)
async def handle_voice_input(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get('mode') != 'voice' or 'direction' not in state:
        await message.answer("Ovozli tarjima uchun avval menyudan '🎙️ Ovoz Tarjimasi' bo'limini tanlang.", reply_markup=get_main_menu_keyboard())
        return
    await process_media(message, message.voice.file_id, "ogg")

@dp.message(F.video)
async def handle_video_input(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not state or state.get('mode') != 'video' or 'direction' not in state:
        await message.answer("Video tarjimasi uchun avval menyudan '🎬 Video Tarjimasi' bo'limini tanlang.", reply_markup=get_main_menu_keyboard())
        return
    if message.video.file_size > 20 * 1024 * 1024:
        await message.answer("Kechirasiz, yuborgan video hajmi 20 MB dan katta.")
        return
    await process_media(message, message.video.file_id, "mp4")

# --- BOTNI ISHGA TUSHIRISH ---
async def main():
    global ocr_reader_latin, ocr_reader_cyrillic, user_states
    
    # Foydalanuvchi holatlarini yuklash
    user_states = load_user_states()
    
    print("EasyOCR modellarini yuklanmoqda...")
    try:
        ocr_reader_latin = easyocr.Reader(['uz', 'en'], gpu=False)
        ocr_reader_cyrillic = easyocr.Reader(['ru'], gpu=False)
        print("EasyOCR modellar tayyor.")
    except Exception as e:
        print(f"EasyOCR'ni ishga tushirishda xatolik: {e}")
        ocr_reader_latin = ocr_reader_cyrillic = None
    
    # Botni ishga tushirish
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Bot qayta ishga tushganligi haqida xabarlarni yuborish
    await notify_users_on_startup()
    
    # Pollingni boshlash
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot to'xtatildi.")
        save_user_states()
