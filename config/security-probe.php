<?php
// Temporary nonce-gated loopback diagnostic. It never emits user configuration.
header('Content-Type: application/json');
$root = dirname(__DIR__) . '/runtime/FreshRSS';
require $root . '/constants.php';
$initialized = is_file(DATA_PATH . '/applied_migrations.txt');
$auth = false;
if ($initialized) {
    require LIB_PATH . '/lib_rss.php';
    FreshRSS_Context::initSystem();
    $c = FreshRSS_Context::systemConf();
    $auth = $c->auth_type === 'form' && $c->allow_anonymous === false
        && $c->allow_anonymous_refresh === false && $c->api_enabled === true
        && $c->disable_update === true && ($c->db['type'] ?? null) === 'sqlite';
}
echo json_encode([
    'sapi' => PHP_SAPI,
    'php' => PHP_VERSION,
    'upload_progress' => ini_get('session.upload_progress.enabled'),
    'expose_php' => ini_get('expose_php'),
    'display_errors' => ini_get('display_errors'),
    'sqlite' => extension_loaded('pdo_sqlite'),
    'loaded_ini' => php_ini_loaded_file(),
    'scanned_ini' => php_ini_scanned_files(),
    'initialized' => $initialized,
    'authenticated_settings' => $auth,
]);
