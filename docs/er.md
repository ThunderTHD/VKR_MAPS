# Модель данных

Модель разбита на пять групп: справочники, инфраструктура (трассы), кабельная часть
(кабели, волокна, сварки), измерения и инциденты, документы и служебные таблицы.

## Принципы

1. **Инфраструктура отделена от кабелей.** Колодцы, опоры и участки трасс принадлежат
   владельцу (Таттелеком, Телесет…), кабели — оператору (Ростелеком, МТС…).
   Кабель проходит по упорядоченной цепочке участков (`cable_route`).
2. **Рассветка нормализована.** Каждая сварка — строка `splice`, связывающая два
   конца волокон. Из этих строк строится граф для трассировки; JSON не используется.
3. **Каждая длина имеет источник** (`length_source`) с погрешностью по умолчанию —
   основа интервала погрешности при локализации и калибровки.
4. **Каждый объект знает своё происхождение**: `source_kind` (manual / kml / asbuilt /
   generator) и `source_ref` (номер импорта или пакета ИД).
5. **Геометрия — EPSG:4326**, длины в метрах через `geography`.

## 1. Справочники

```mermaid
erDiagram
    owner {
        int id PK
        text name
        text short_name
        text color
    }
    operator {
        int id PK
        text name
        text color
    }
    length_source {
        text code PK
        text name
        float default_error_pct
        int priority
    }
    color_scheme {
        int id PK
        text name
    }
    color_scheme_entry {
        int scheme_id FK
        text kind "module|fiber"
        int position
        text color_name
        text hex
    }
    cable_type {
        int id PK
        text mark
        int modules
        int fibers_per_module
        int fiber_count
        int color_scheme_id FK
    }

    color_scheme ||--o{ color_scheme_entry : "цвета"
    color_scheme ||--o{ cable_type : "маркировка"
```

`length_source`: `reflectometer` 1 %, `as_built` 2 %, `passport` 5 %, `engineer` 10 %,
`geometry` 25 %. `priority` определяет, какая длина берётся, если известно несколько.

Стандартная схема из 8 цветов: синий, оранжевый, зелёный, коричневый, серый, белый,
красный, чёрный (например, ОКГ-8, ОКЛ-8 в ИД Чапаева 62Б).

## 2. Инфраструктура

```mermaid
erDiagram
    network_node {
        int id PK
        text kind "well|kks|pole|site|building|unknown"
        text code "из схемы: 57121, к.№348, 02-17"
        text name "редактирует админ"
        int owner_id FK
        geometry geom "Point 4326"
        text note
        text source_kind
        int source_ref
    }
    route_segment {
        int id PK
        text kind "duct|microduct|aerial|facade|indoor|hdd"
        int owner_id FK
        int node_a_id FK
        int node_b_id FK
        geometry geom "LineString 4326"
        float geom_length_m "по геометрии"
        float fact_length_m "фактическая"
        text length_source FK
        int length_updated_by FK
        timestamptz length_updated_at
        int channels_total
        text source_kind
        int source_ref
    }
    owner ||--o{ network_node : "владеет"
    owner ||--o{ route_segment : "владеет"
    network_node ||--o{ route_segment : "конец A"
    network_node ||--o{ route_segment : "конец B"
```

Узел (`network_node`) — любая точка сети: колодец, ККС, опора, площадка БС, здание.
`code` нужен для привязки при импорте KML («к.№348 - к.№498»), `name` — для людей.

## 3. Кабели, волокна, сварки

```mermaid
erDiagram
    cable {
        int id PK
        int operator_id FK
        int cable_type_id FK
        text name "СЛ 16000149/03"
        float twist_factor "укрутка, 0.0045 = 0,45 %"
        int start_node_id FK
        int end_node_id FK
        text source_kind
    }
    cable_route {
        int cable_id FK
        int seq
        int segment_id FK
        smallint direction "+1 / -1"
    }
    channel_occupancy {
        int segment_id FK
        int cable_id FK
        int channel_no
    }
    cable_reserve {
        int id PK
        int cable_id FK
        int node_id FK
        float length_m
        text length_source FK
    }
    fiber {
        int id PK
        int cable_id FK
        int module_no
        int fiber_no
        text color
    }
    closure {
        int id PK
        int node_id FK
        text number "М1"
        text kind
        text note
    }
    optical_cross {
        int id PK
        int node_id FK
        text name "ODF-1"
        text model
        int port_count
        text connector
    }
    cross_port {
        int id PK
        int cross_id FK
        int port_no
        int fiber_id FK
        text fiber_end "start|end"
    }
    splice {
        int id PK
        int closure_id FK "или"
        int cross_id FK "или"
        int fiber_a_id FK
        text end_a "start|end"
        int fiber_b_id FK
        text end_b "start|end"
        float loss_splicer_db
        float loss_otdr_ab_db
        float loss_otdr_ba_db
    }

    cable ||--o{ cable_route : "проходит по"
    route_segment ||--o{ cable_route : "участок"
    cable ||--o{ cable_reserve : "запасы"
    cable ||--|{ fiber : "волокна"
    network_node ||--o{ closure : "муфты"
    network_node ||--o{ optical_cross : "кроссы"
    optical_cross ||--o{ cross_port : "порты"
    closure ||--o{ splice : "сварки"
    optical_cross ||--o{ splice : "сварки (пигтейлы)"
    fiber ||--o{ splice : "концы"
    fiber ||--o| cross_port : "на порт"
```

**Концы волокна.** У каждого волокна два конца: `a` — у начала кабеля (start_node) и
`b` — у конца (end_node). Конец может участвовать не более чем в одной сварке или быть заведён не более
чем на один порт кросса — это и есть правило «одно волокно — одна сварка».
Проверяется триггерами PL/pgSQL на `splice` и `cross_port` (миграция 0003): база
отклоняет повторное подключение конца волокна при любом способе записи.

**Пример из ИД Чапаева 62Б:** ODF-4 порт 78 → ДПО-П-08У ОВ 3 → сварка в УД
16ELBG-ACCF00-173 → ОКЛ-8 ОВ 2 → сварка в муфте М1 → ОКГ-8 ОВ 1 → ODF-1 порт 1.

## 4. Узлы доступа, измерения, инциденты

```mermaid
erDiagram
    access_node {
        int id PK
        int node_id FK
        text hostname
        inet ip
        text model
        text serial
        int uplink_id FK "вышестоящий access_node"
        text uplink_port
        text status "up|down|unknown"
        int fail_count
        timestamptz status_changed_at
    }
    node_status_history {
        bigint id PK
        int access_node_id FK
        timestamptz ts
        text status
        float rtt_ms
    }
    instrument {
        int id PK
        text kind "otdr|power_meter|splicer"
        text model
        text registry_no
        text serial
        date calibration_valid_until
        int calibration_doc_id FK
    }
    measurement {
        int id PK
        text kind "otdr|power_meter"
        int port_a_id FK
        int port_b_id FK
        int wavelength_nm
        float refractive_index
        float optical_length_m
        float loss_ab_db
        float loss_ba_db
        int instrument_id FK
        date measured_at
        int package_id FK
    }
    incident {
        int id PK
        int fiber_id FK
        int measure_port_id FK
        float otdr_distance_m
        geometry predicted_geom
        float predicted_error_m
        geometry actual_geom
        float error_m "факт − прогноз"
        timestamptz detected_at
        timestamptz arrived_at
        timestamptz restored_at
        int executor_id FK
        text status
    }
    calibration_run {
        int id PK
        timestamptz created_at
        jsonb params
        jsonb metrics_before
        jsonb metrics_after
        int approved_by FK
        timestamptz applied_at
    }

    access_node ||--o{ node_status_history : "история"
    access_node |o--o{ access_node : "uplink"
    instrument ||--o{ measurement : "прибор"
    calibration_run }o--o{ incident : "по инцидентам"
    calibration_run }o--o{ measurement : "по измерениям ИД"
```

`sim_truth` — «истинные» значения синтетической сети (длины участков и запасов, укрутка
кабелей, оптические длины трактов). Заполняется только генератором и используется
только в эксперименте: система её не читает.

`measurement` из пакетов ИД — эталон для калибровки: оптическая длина тракта измерена
прибором (в примере 2,627 км), а система считает её по графу и сравнивает.

## 5. Документы, импорт, служебные

```mermaid
erDiagram
    app_user {
        int id PK
        text login
        text full_name
        text role "admin|installer|viewer"
        text password_hash
        bool active
    }
    audit_log {
        bigint id PK
        timestamptz ts
        int user_id FK
        text action
        text entity_type
        int entity_id
        jsonb before
        jsonb after
    }
    document {
        int id PK
        text owner_type
        int owner_id
        text kind
        text filename
        text path
        text mime
        int size
        int uploaded_by FK
    }
    asbuilt_package {
        int id PK
        text volp
        text object_name
        text contractor
        date doc_date
        int document_id FK
        jsonb parse_result
        jsonb check_report
        text status
    }
    import_job {
        int id PK
        text kind "kml|asbuilt|docx|shp"
        text filename
        int user_id FK
        text status
        jsonb folder_mapping
        jsonb report
    }
    kml_folder_rule {
        int id PK
        text folder_name
        text target_kind
        int owner_id FK
    }
    unclassified_feature {
        int id PK
        int import_job_id FK
        text folder
        text name
        geometry geom
    }
    map_overlay {
        int id PK
        text name
        int document_id FK
        jsonb control_points
        float opacity
    }
    jumper {
        int id PK
        text a_type
        int a_id
        text b_type
        int b_id
        geometry geom
        text label
    }

    app_user ||--o{ audit_log : "действия"
    import_job ||--o{ unclassified_feature : "без типа"
    asbuilt_package ||--o| document : "исходный PDF"
    document ||--o| map_overlay : "подложка"
```

`document` и `jumper` ссылаются на объекты полиморфно (`owner_type` + `owner_id`):
файл можно прикрепить к муфте, узлу, кабелю или пакету ИД одной таблицей.

`parse_result` и `check_report` в пакете ИД — JSON: это сырой результат разбора и
отчёт проверки (как они пришли из документа). Данные сети из них переносятся в
нормализованные таблицы только после подтверждения пользователем.
