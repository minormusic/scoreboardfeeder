<?php
// DB-tunnukset — tämä tiedosto estetään .htaccess:lla
define('DB_HOST', 'localhost');
define('DB_USER', 'minormusic_palloliitto');
define('DB_PASSWORD', ''); // aseta palvelimella
define('DB_NAME', 'minormusic_palloliitto');

// Kenttä jonka otteluita näytetään
define('DEFAULT_VENUE_SLUG', 'oulunkyla');

// Gnistan-priorisointi live-otteluissa
define('PRIORITY_TEAM', 'gnistan');
