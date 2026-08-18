(() => {
    const statusCache = new Map();

    const moneyFormatter = new Intl.NumberFormat(
        "en-US",
        {
            style: "currency",
            currency: "USD",
        }
    );

    const extractProductId = (href) => {
        const match = String(href || "").match(
            /\/products\/(\d+)/
        );

        return match ? Number(match[1]) : null;
    };

    const loadStatus = async (productId) => {
        if (statusCache.has(productId)) {
            return statusCache.get(productId);
        }

        const request = fetch(
            `/api/products/${productId}/channel-status`
        )
            .then((response) => {
                if (!response.ok) {
                    throw new Error(
                        `Channel status HTTP ${response.status}`
                    );
                }

                return response.json();
            })
            .catch((error) => {
                console.error(
                    `Could not load channel status for product ${productId}:`,
                    error
                );

                return null;
            });

        statusCache.set(
            productId,
            request
        );

        return request;
    };

    const badgeClass = (
        channel,
        channelStatus
    ) => {
        const status = (
            channelStatus.status
            || "not_linked"
        );

        return [
            "sales-channel-badge",
            `sales-channel-${channel}`,
            `sales-channel-${status}`,
        ].join(" ");
    };

    const createBadge = (
        channel,
        channelStatus
    ) => {
        const badge = document.createElement(
            "span"
        );

        badge.className = badgeClass(
            channel,
            channelStatus
        );

        badge.textContent =
            channelStatus.label;

        const details = [];

        if (channel === "amazon") {
            if (channelStatus.asin) {
                details.push(
                    `ASIN: ${channelStatus.asin}`
                );
            }

            if (channelStatus.seller_sku) {
                details.push(
                    `SKU: ${channelStatus.seller_sku}`
                );
            }
        }

        if (channel === "walmart") {
            if (channelStatus.item_id) {
                details.push(
                    `Item ID: ${channelStatus.item_id}`
                );
            }

            if (channelStatus.seller_sku) {
                details.push(
                    `SKU: ${channelStatus.seller_sku}`
                );
            }
        }

        if (
            channelStatus.quantity !== null
            && channelStatus.quantity !== undefined
        ) {
            details.push(
                `Qty: ${channelStatus.quantity}`
            );
        }

        if (
            channelStatus.price !== null
            && channelStatus.price !== undefined
        ) {
            details.push(
                `Price: ${moneyFormatter.format(
                    Number(channelStatus.price)
                )}`
            );
        }

        if (details.length) {
            badge.title = details.join(
                " | "
            );
        }

        return badge;
    };

    const createContainer = (
        productId
    ) => {
        const container = document.createElement(
            "div"
        );

        container.className =
            "sales-channel-badges";

        container.dataset.productId =
            String(productId);

        return container;
    };

    const choosePlacementTarget = (
        link
    ) => {
        return (
            link.closest(
                "tr, article, .product-card, .scan-result, .search-result, .dashboard-panel"
            )
            || link.parentElement
        );
    };

    const addStatusToTarget = async (
        target,
        productId
    ) => {
        if (!target || !productId) {
            return;
        }

        if (
            target.querySelector(
                `.sales-channel-badges[data-product-id="${productId}"]`
            )
        ) {
            return;
        }

        const container = createContainer(
            productId
        );

        target.appendChild(
            container
        );

        const status = await loadStatus(
            productId
        );

        if (!status) {
            container.remove();
            return;
        }

        container.appendChild(
            createBadge(
                "amazon",
                status.amazon
            )
        );

        container.appendChild(
            createBadge(
                "walmart",
                status.walmart
            )
        );
    };

    const addBadgesToProductLinks = () => {
        const links = Array.from(
            document.querySelectorAll(
                'a[href^="/products/"]'
            )
        );

        const processed = new Set();

        links.forEach((link) => {
            const productId =
                extractProductId(
                    link.getAttribute("href")
                );

            if (!productId) {
                return;
            }

            const target =
                choosePlacementTarget(link);

            const key = `${
                productId
            }-${
                target.tagName
            }-${
                Array.from(
                    document.querySelectorAll("*")
                ).indexOf(target)
            }`;

            if (processed.has(key)) {
                return;
            }

            processed.add(key);

            addStatusToTarget(
                target,
                productId
            );
        });
    };

    const addBadgeToProductDetail = () => {
        const match = window.location.pathname.match(
            /^\/products\/(\d+)\/?$/
        );

        if (!match) {
            return;
        }

        const productId = Number(
            match[1]
        );

        const heading =
            document.querySelector(
                "main h1, main h2, .page-container h1, .page-container h2"
            );

        if (!heading) {
            return;
        }

        const wrapper =
            document.createElement(
                "section"
            );

        wrapper.className =
            "product-channel-status-panel";

        const title =
            document.createElement(
                "h3"
            );

        title.textContent =
            "Sales Channel Status";

        wrapper.appendChild(
            title
        );

        heading.insertAdjacentElement(
            "afterend",
            wrapper
        );

        addStatusToTarget(
            wrapper,
            productId
        );
    };

    const initialize = () => {
        addBadgeToProductDetail();
        addBadgesToProductLinks();
    };

    if (
        document.readyState === "loading"
    ) {
        document.addEventListener(
            "DOMContentLoaded",
            initialize
        );
    } else {
        initialize();
    }
})();
