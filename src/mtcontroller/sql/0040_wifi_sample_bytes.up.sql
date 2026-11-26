-- Přenesená data Wi-Fi klienta v čase: čítače z registrační tabulky AP (od připojení; „bytes“ = odesláno,přijato
-- z pohledu AP). Z rozdílů sousedních vzorků se počítá provoz klienta v rozboru připojení.
ALTER TABLE wifi_sample ADD COLUMN tx_bytes bigint, ADD COLUMN rx_bytes bigint;
