<?php

namespace Drupal\tamu_metadata_api\Controller;

use Drupal\Core\Controller\ControllerBase;
use Drupal\Core\Database\Connection;
use Symfony\Component\DependencyInjection\ContainerInterface;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/**
 * Lists chosen metadata keys of every Digital Object, a page at a time.
 *
 * GET /api/metadata-values?keys=subject,creator&after=0&limit=5000
 *
 * Returns {"rows": [{"nid", "uuid", "bundle", "title", "published",
 * "metadata": {...}}],
 * "next": <nid to pass as "after" for the next page, or null when done>}.
 * Only Digital Objects holding at least one of the keys are listed, and
 * "metadata" holds only the keys each one actually has. "published" is the
 * node's published flag, so a caller can leave unpublished ADOs alone.
 *
 * One read-only query per page against the Strawberry Field table, so a
 * whole repository comes back in a handful of requests. JSON:API caps a page
 * at 50 nodes and loads and serializes every one in full, which is what makes
 * it slow for this. Nothing here writes, and no SQL is taken from the
 * request: keys come from a fixed list and every value is a placeholder.
 */
class MetadataValuesController extends ControllerBase {

  /**
   * The keys that can be asked for. Must match KEYS in value_mapper/mapping.py;
   * tests/test_keys_in_sync.py checks it.
   */
  const KEYS = [
    'creator',
    'creator_linked_data',
    'contributor',
    'contributor_linked_data',
    'agent',
    'agent_linked_data',
    'subject',
    'subject_linked_data',
    'geographic_subject',
    'geographic_subject_linked_data',
    'temporal_subject',
    'temporal_subject_linked_data',
    'genre_form',
    'genre_form_linked_data',
  ];

  const BUNDLES = ['digital_object', 'digital_object_collection'];

  const MAX_LIMIT = 5000;

  public function __construct(protected Connection $database) {}

  public static function create(ContainerInterface $container) {
    return new static($container->get('database'));
  }

  public function values(Request $request): JsonResponse {
    $keys = array_values(array_unique(array_filter(array_map(
      'trim', explode(',', (string) $request->query->get('keys', ''))
    ))));
    if (!$keys) {
      return $this->error('keys is required, e.g. ?keys=subject,creator');
    }
    $unknown = array_diff($keys, self::KEYS);
    if ($unknown) {
      return $this->error('unknown key(s): ' . implode(', ', $unknown) . '. Allowed: ' . implode(', ', self::KEYS));
    }
    $after = (string) $request->query->get('after', '0');
    if (!ctype_digit($after)) {
      return $this->error('after must be a node id');
    }
    $limit = (string) $request->query->get('limit', (string) self::MAX_LIMIT);
    if (!ctype_digit($limit) || (int) $limit < 1 || (int) $limit > self::MAX_LIMIT) {
      return $this->error('limit must be between 1 and ' . self::MAX_LIMIT);
    }

    $column = 'm.field_descriptive_metadata_value';
    $args = [':after' => (int) $after, ':bundles[]' => self::BUNDLES];
    $selected = [];
    $contains = [];
    foreach ($keys as $index => $key) {
      $selected[] = "JSON_EXTRACT($column, :path$index) AS k$index";
      $contains[] = ":has$index";
      $args[":path$index"] = '$.' . $key;
      $args[":has$index"] = '$.' . $key;
    }
    $sql = 'SELECT m.entity_id AS nid, m.bundle, n.uuid, d.title, d.status, ' . implode(', ', $selected)
      . ' FROM {node__field_descriptive_metadata} m'
      . ' INNER JOIN {node} n ON n.nid = m.entity_id'
      . ' LEFT JOIN {node_field_data} d ON d.nid = m.entity_id AND d.langcode = m.langcode'
      . ' WHERE m.deleted = 0 AND m.bundle IN (:bundles[]) AND m.entity_id > :after'
      . " AND JSON_CONTAINS_PATH($column, 'one', " . implode(', ', $contains) . ')'
      . ' ORDER BY m.entity_id';

    $rows = [];
    foreach ($this->database->queryRange($sql, 0, (int) $limit, $args) as $record) {
      $metadata = new \stdClass();
      foreach ($keys as $index => $key) {
        $raw = $record->{"k$index"};
        if ($raw === NULL) {
          continue;
        }
        // Decoded to objects, not arrays, so {"value": ...} entries go back
        // out as JSON objects. MariaDB has no JSON type and hands back text;
        // MySQL's JSON type does too through PDO, so this covers both.
        $value = json_decode($raw);
        if (json_last_error() === JSON_ERROR_NONE) {
          $metadata->{$key} = $value;
        }
      }
      $rows[] = [
        'nid' => (int) $record->nid,
        'uuid' => $record->uuid,
        'bundle' => $record->bundle,
        'title' => $record->title,
        'published' => (int) $record->status === 1,
        'metadata' => $metadata,
      ];
    }

    $next = count($rows) === (int) $limit ? end($rows)['nid'] : NULL;
    return $this->respond(['rows' => $rows, 'next' => $next]);
  }

  protected function error(string $message): JsonResponse {
    return $this->respond(['error' => $message], 400);
  }

  protected function respond(array $data, int $status = 200): JsonResponse {
    $response = new JsonResponse($data, $status);
    $response->setEncodingOptions(JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    $response->headers->set('Cache-Control', 'no-store, private');
    return $response;
  }

}
