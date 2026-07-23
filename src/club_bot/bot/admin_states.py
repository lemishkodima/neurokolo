from aiogram.fsm.state import State, StatesGroup


class PlanCreateStates(StatesGroup):
    name = State()
    price = State()


class PlanEditStates(StatesGroup):
    name = State()
    price = State()


class AdminAddStates(StatesGroup):
    telegram_id = State()


class SettingEditStates(StatesGroup):
    value = State()


class LandingTemplateCreateStates(StatesGroup):
    name = State()
    slug = State()
    landing_title = State()
    channel_title = State()
    landing_description = State()
    html_template = State()


class LandingTemplateEditStates(StatesGroup):
    value = State()


class MenuContentStates(StatesGroup):
    content = State()
    buttons = State()


class BroadcastCreateStates(StatesGroup):
    content = State()
    buttons = State()
    target = State()
    confirm = State()
