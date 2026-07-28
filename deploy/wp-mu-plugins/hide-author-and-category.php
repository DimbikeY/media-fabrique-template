<?php
/**
 * Plugin Name: Hide Author and Category Meta
 * Description: Removes the "Written by X in Y" block under every post. We don't want bot author mentions (Media Fabrique Bot leaks that posts are automated) or category labels (Uncategorized) on the public site.
 * Version:     1.0.0
 * Author:      Media Fabrique
 *
 * Why this lives in mu-plugins (not the theme):
 *   * Survives theme updates / wp-core upgrades — we never have to re-apply it.
 *   * Theme-agnostic. If DD switches Twenty Twenty-Five to something else later,
 *     the rule still applies because it operates on WP blocks, not theme files.
 *   * mu-plugins are loaded before regular plugins, so block editors / cache
 *     warmers can't accidentally cache the unwanted markup.
 *
 * Strategy: hook `render_block` and strip the two block types we care about:
 *   * `core/post-author-name` — renders the author name (e.g. "Media Fabrique Bot")
 *   * `core/post-terms`        — when term=category, renders "in Uncategorized"
 *
 * We intentionally do NOT touch `core/post-terms` for `post_tag` — tags are
 * useful for readers (e.g. "OpTic Texas", "CoD"). Only the category block is
 * suppressed because the WP REST API requires every post to have a category,
 * so we can't just leave it empty.
 *
 * If DD later decides to bring author names back, just delete this file
 * (or `mv` it to wp-content/plugins/` and disable it from wp-admin).
 */

add_filter('render_block', function (string $block_content, array $block): string {
    $name = $block['blockName'] ?? '';

    if ($name === 'core/post-author-name') {
        return '';
    }

    if ($name === 'core/post-terms') {
        $attrs = $block['attrs'] ?? [];
        $term  = $attrs['term'] ?? '';
        if ($term === 'category') {
            return '';
        }
    }

    // The "Written by ... in ..." line in Twenty Twenty-Five is a reusable
    // pattern (slug `twentytwentyfive/hidden-written-by`) that contains a
    // static "Written by " paragraph plus the author + category blocks above.
    // Stripping the inner blocks leaves the static paragraph orphaned, so we
    // also strip the whole pattern when it matches our target slug.
    if ($name === 'core/pattern') {
        $slug = $block['attrs']['slug'] ?? '';
        if ($slug === 'twentytwentyfive/hidden-written-by') {
            return '';
        }
    }

    return $block_content;
}, 10, 2);
