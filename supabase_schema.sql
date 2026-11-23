-- Kør én gang i Supabase SQL Editor for at oprette tabellen, som forsinkelser.py skriver til.
-- (Findes tabellen allerede med de gamle _valby-kolonner, så kør rename_columns.sql i stedet.)

create table if not exists train_delays (
    service_date date not null,
    train_number text not null,
    train_name text not null,
    scheduled_departure_roskilde timestamp not null,
    actual_departure_roskilde timestamp not null,
    departure_delay_minutes integer not null,
    scheduled_arrival_holbaek timestamp not null,
    actual_arrival_holbaek timestamp not null,
    arrival_delay_minutes integer not null,
    cancelled boolean not null default false,
    fetched_at timestamptz not null default now(),
    primary key (service_date, train_number)
);

-- Spærrer for adgang med den offentlige anon-nøgle. Scriptet skriver med
-- service_role-nøglen, som går uden om RLS.
alter table train_delays enable row level security;
