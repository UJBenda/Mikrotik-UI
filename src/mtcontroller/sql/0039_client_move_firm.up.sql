-- Přeskakování MAC počítat jen z jistých poloh: klient na koncovém portu. Poloha odhadnutá na páteřním portu
-- (uplink, bonding, VLAN rozhraní, spoj) se mezi inventurami mění sama a smyčku neznamená.
-- Starší řádky zůstávají „nejisté“ – stávající plané poplachy tím po aktualizaci zmizí.
ALTER TABLE client_move ADD COLUMN firm boolean NOT NULL DEFAULT false;
