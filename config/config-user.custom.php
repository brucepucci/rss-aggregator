<?php
// Applied by upstream to new users. No per-feed/category purge overrides in this slice.
return [
    'archiving' => ['keep_period' => false, 'keep_max' => false],
    // Below the 20-minute scheduler interval, avoiding its <= TTL boundary.
    'ttl_default' => 60,
];
