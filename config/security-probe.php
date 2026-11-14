<?php
// Requested only over container loopback while application serving is disabled.
// Outputs runtime metadata only; removed before the application listener opens.
header('Content-Type: application/json');
echo json_encode([
    'sapi' => PHP_SAPI,
    'php' => PHP_VERSION,
    'upload_progress' => ini_get('session.upload_progress.enabled'),
    'loaded_ini' => php_ini_loaded_file(),
    'scanned_ini' => php_ini_scanned_files(),
]);
