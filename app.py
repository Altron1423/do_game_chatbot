from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from game import Game, game


BASE_DIR = Path(__file__).resolve().parent
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "tankcity"
security = HTTPBasic()
state_condition = asyncio.Condition()
room_wakeups: dict[str, asyncio.Event] = {}
room_tasks: dict[str, asyncio.Task] = {}


class RoomCreate(BaseModel):
    name: str = Field(default="Комната", min_length=1, max_length=60)


class JoinRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class MessageRequest(BaseModel):
    sender: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=500)


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    user_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    password_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (user_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль.",
            headers={"WWW-Authenticate": "Basic"},
        )


def find_room(room_id: str):
    room = game.rooms.get(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Комната не найдена.")
    return room


app = FastAPI(title="Танковый чат")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


async def publish_room_event(room_id: str) -> None:
    room = game.rooms.get(room_id)
    if room is not None:
        room.state_version += 1
        if room_id not in room_tasks or room_tasks[room_id].done():
            wakeup = asyncio.Event()
            room_wakeups[room_id] = wakeup
            room_tasks[room_id] = asyncio.create_task(room_event_loop(room_id, wakeup))
        room_wakeups[room_id].set()
    elif room_id in room_wakeups:
        room_wakeups[room_id].set()
    async with state_condition:
        state_condition.notify_all()


async def room_event_loop(room_id: str, wakeup: asyncio.Event) -> None:
    try:
        while True:
            room = game.rooms.get(room_id)
            if room is None:
                return
            wakeup.clear()
            delay = game.next_room_event_delay(room)
            try:
                if delay is None:
                    await wakeup.wait()
                else:
                    await asyncio.wait_for(wakeup.wait(), timeout=max(delay, 0.001))
                continue
            except TimeoutError:
                room = game.rooms.get(room_id)
                if room is None:
                    return
                game.process_room_events(room)
                room.state_version += 1
                async with state_condition:
                    state_condition.notify_all()
    finally:
        if room_tasks.get(room_id) is asyncio.current_task():
            room_tasks.pop(room_id, None)
            room_wakeups.pop(room_id, None)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(BASE_DIR / "static" / "admin.html")


@app.get("/api/rooms")
async def list_rooms():
    return [game.room_summary(room) for room in game.rooms.values()]


@app.post("/api/rooms", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def create_room(payload: RoomCreate):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Название комнаты не может быть пустым.")
    room = game.create_room(name)
    await publish_room_event(room.id)
    return game.room_summary(room)


@app.delete("/api/rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin)])
async def delete_room(room_id: str):
    if not game.delete_room(room_id):
        raise HTTPException(status_code=404, detail="Комната не найдена.")
    await publish_room_event(room_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, payload: JoinRequest):
    room = find_room(room_id)
    try:
        game.join(room, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await publish_room_event(room_id)

    tank = next(
        (t for t in room.tanks
         if not t.is_bot and t.name.casefold() == payload.name.strip().casefold()),
        None,
    )
    return {
        **game.room_summary(room),
        "your_color": tank.color if tank else None,
        "your_tank_id": tank.id if tank else None,
    }


@app.post("/api/rooms/{room_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(room_id: str, payload: MessageRequest):
    room = find_room(room_id)
    try:
        game.process_room_events(room)
        game.post_message(room, payload.sender, payload.text)
        game.process_room_events(room)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await publish_room_event(room_id)
    # Ничего не возвращаем — клиенту не нужен ответ.
    return Response(status_code=status.HTTP_201_CREATED)


@app.post("/api/rooms/{room_id}/commands", status_code=status.HTTP_201_CREATED)
async def send_command(room_id: str, payload: MessageRequest):
    room = find_room(room_id)
    normalized = payload.text.strip().casefold()
    if normalized not in {"вверх", "вниз", "влево", "вправо", "выстрел"}:
        raise HTTPException(status_code=422, detail="Неизвестная команда.")
    try:
        game.process_room_events(room)
        game.post_message(room, payload.sender, normalized)
        game.process_room_events(room)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await publish_room_event(room_id)
    return Response(status_code=status.HTTP_201_CREATED)


@app.get("/api/rooms/{room_id}/state")
async def room_state(room_id: str, after: Optional[int] = Query(default=None, ge=0)):
    room = find_room(room_id)
    if after is not None and room.state_version == after:
        async with state_condition:
            await state_condition.wait_for(
                lambda: room.state_version != after or game.rooms.get(room_id) is not room
            )
    if game.rooms.get(room_id) is not room:
        raise HTTPException(status_code=404, detail="Комната не найдена.")
    return game.room_state(room)