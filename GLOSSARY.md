# Personal feed reading

The language for a self-hosted reading service that collects public and paid feeds and shares reading state across devices.

## Language

### Reading service

**FreshRSS instance**:
The self-hosted reading service that holds users, subscriptions, collected articles, and reading state.
_Avoid_: Feed reader, subscription

**FreshRSS user**:
An identity within a FreshRSS instance that owns subscriptions and reading state; distinct from a publication membership or a device's reader configuration.
_Avoid_: Account (without specifying which service)

**Feed reader**:
An application through which a FreshRSS user reads collected articles and changes reading state.
_Avoid_: Server

### Sources and subscriptions

**Feed**:
A publisher-provided stream of articles or article updates.
_Avoid_: Subscription

**Subscription**:
A FreshRSS user's relationship to a feed they have chosen to follow.
_Avoid_: Feed, feed URL

**Article**:
An individual entry collected from a feed, with the content made available by that feed; it need not contain the publisher's full web page.
_Avoid_: Feed, subscription

**Public feed**:
A feed available without access credentials.
_Avoid_: Free feed (when describing access rather than price)

**Paid feed**:
A feed whose availability depends on a paid publication membership or subscription; this describes entitlement, not its authentication mechanism.
_Avoid_: Private feed (when only payment is meant)

**Private feed**:
A feed whose access requires a credential or subscriber-specific URL; it is not necessarily paid.
_Avoid_: Paid feed (when only restricted access is meant)

**Private feed URL**:
A feed address that itself grants access to restricted content and must be treated as a credential.
_Avoid_: Ordinary link, public feed URL

### Updates and reading state

**Feed fetching**:
The FreshRSS instance's retrieval of new or updated articles from a publisher's feed, separate from synchronization with a feed reader.
_Avoid_: Client synchronization, crawling (when only feed retrieval is meant)

**Client synchronization**:
The exchange of articles and reading-state changes between a feed reader and the FreshRSS instance, separate from fetching updates from publishers.
_Avoid_: Feed fetching, refresh (without specifying which operation)

**Reading state**:
A FreshRSS user's read/unread and starred/unstarred markings for an article, shared across their feed readers.
_Avoid_: Client-local state (for the shared markings)

### Recovery

**Instance backup**:
A recovery copy of the complete FreshRSS state, including users, settings, subscriptions, articles, and reading state.
_Avoid_: Subscription export, feed list

**Subscription export**:
A transferable list of feeds being followed, not a recovery copy of articles, users, settings, or reading state.
_Avoid_: Instance backup
