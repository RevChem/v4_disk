from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional


class ViolationRowStatusUpdate(BaseModel):
    """Тело запроса на изменение статуса строки нарушения (кнопка «Отработано»)."""

    key:    str = Field(..., max_length=255, description="Ключ строки (например 'c1_3_482910')")
    status: int = Field(..., ge=0, le=2, description="0 = В работу, 1 = В работе, 2 = Отработана")


class ViolationRowStatusBulkUpdate(BaseModel):
    """Массовая смена статуса строк нарушений (кнопка «Отметить все» в модалке «URL»)."""

    keys:   list[str] = Field(..., min_length=1, max_length=2000, description="Ключи строк")
    status: int = Field(..., ge=0, le=2, description="0 = В работу, 1 = В работе, 2 = Отработана")

    @field_validator("keys")
    @classmethod
    def _keys_ok(cls, v: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(k for k in v if k))  # без пустых и дублей, порядок сохраняем
        if not cleaned:
            raise ValueError("Список ключей пуст")
        if any(len(k) > 255 for k in cleaned):
            raise ValueError("Ключ строки длиннее 255 символов")
        return cleaned


class ViolationResolutionItem(BaseModel):
    """Одна строка статистики «кто сколько нарушений отработал» за период."""

    user_id:   int = Field(..., description="ID пользователя")
    user_fio:  str = Field(..., description="ФИО пользователя")
    user_role: str = Field(..., description="Роль пользователя")
    resolved:  int = Field(..., description="Кол-во строк со статусом «Отработана» за период")


class ViolationResolutionResponse(BaseModel):
    items: list[ViolationResolutionItem]


class ViolationsRefreshRequest(BaseModel):
    """Тело POST /api/violations/refresh — выборочное обновление checks."""

    checks: list[str] | None = Field(
        None,
        description=(
            "Список checks для обновления, например ['check3','check6']. "
            "None или пустой список — все проверки. check6 автоматически включает check6t."
        ),
    )


class CropwiseUserItem(BaseModel):
    """Один пользователь с привязанной машиной и заданием."""

    username:        str = Field(..., description="ФИО / username из Cropwise")
    phone:           str = Field(..., description="Телефон пользователя")
    reg_number:      str = Field(..., description="Рег. номер привязанной машины")
    machine_subtype: str = Field(..., description="Подтип машины")
    work_type:       str = Field(..., description="Тип работы")
    task_label:      str = Field(..., description="Метка задания (ID + описание)")


class CropwiseUsersFilters(BaseModel):
    hours:           Optional[int] = Field(None, description="Фильтр по последним N часам")
    filter_username: Optional[str] = Field(None, description="Фильтр по ФИО (подстрока)")
    machine_subtype: Optional[str] = Field(None, description="Фильтр по подтипу машины")
    work_type:       Optional[str] = Field(None, description="Фильтр по типу работы (подстрока)")


class CropwiseUsersResponse(BaseModel):
    total:   int                    = Field(..., description="Кол-во записей")
    filters: CropwiseUsersFilters   = Field(..., description="Применённые фильтры")
    items:   list[CropwiseUserItem] = Field(..., description="Список пользователей")


class SpeedImplementIn(BaseModel):
    """Оборудование под контролем скорости (модалка «Жатки», вкладка «Превышение скорости»)."""

    implement_id:     int = Field(..., gt=0, description="ID оборудования в Cropwise (implement_id)")
    name:             Optional[str]   = Field(None, max_length=255, description="Название")
    inventory_number: Optional[str]   = Field(None, max_length=64,  description="Инвентарный номер")
    speed_min:        Optional[float] = Field(None, ge=0, le=100, description="Оптимальная скорость: от, км/ч")
    speed_max:        float = Field(..., gt=0, le=100, description="Оптимальная скорость: до, км/ч — порог превышения")

    @field_validator("name", "inventory_number", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @model_validator(mode="after")
    def _range_ok(self):
        if self.speed_min is not None and self.speed_min > self.speed_max:
            raise ValueError("Скорость «от» не может быть больше скорости «до»")
        return self
