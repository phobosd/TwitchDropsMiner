from datetime import datetime, timedelta, timezone

from src.config.settings import Settings
from src.models.campaign import DropsCampaign
from src.models.game import Game


class StreamSelector:
    def _get_wanted_game_tree(
        self,
        settings: Settings,
        campaigns: list[DropsCampaign],
        *,
        for_display: bool = False,
    ) -> list[dict]:
        """
        Get the hierarchical tree of wanted items (Games -> Campaigns -> Drops -> Benefits).
        Ignoring 'can earn within' time constraint.

        When ``for_display`` is set (UI use) the tree also includes campaigns/drops
        that the miner won't earn:

        * campaigns that are otherwise earnable but need an account link, flagged
          with ``needs_account_link`` and their ``link_url``;
        * purchase/subscription rewards (no watch minutes), flagged with
          ``requires_purchase``; their campaign carries a ``channels_url`` with the
          participating-channels directory so the user can go buy/subscribe.

        Mining keeps the eligible, watch-only path so these are never watched.
        """
        wanted_games = []
        games_to_watch = settings.games_to_watch
        mining_benefits = settings.mining_benefits
        now = datetime.now(timezone.utc)
        next_hour = now + timedelta(hours=1)

        for game_name in games_to_watch:
            wanted_campaigns = []
            game_obj = None
            game_name_lower = game_name.lower()

            # Find all campaigns for this game
            for campaign in campaigns:
                if campaign.game.name.lower() != game_name_lower:
                    continue

                if game_obj is None:
                    game_obj = campaign.game

                if not campaign.can_earn_within(
                    next_hour,
                    ignore_eligibility=for_display,
                    include_purchase=for_display,
                ):
                    continue

                wanted_drops = []
                has_purchase_reward = False
                for drop in campaign.drops:
                    if drop.is_claimed or drop.ends_at <= now:
                        continue

                    if drop.is_watch_drop:
                        if not drop.is_mineable:
                            continue
                        filtered_benefits = drop.get_wanted_unclaimed_benefits(mining_benefits)
                    else:
                        # Purchase/subscription reward: display-only, not mineable.
                        if not for_display:
                            continue
                        filtered_benefits = [
                            benefit.name
                            for benefit in drop.benefits
                            if benefit.is_wanted(mining_benefits)
                        ]

                    if len(filtered_benefits) > 0:
                        drop_node = {"name": drop.name, "benefits": filtered_benefits}
                        if not drop.is_watch_drop:
                            drop_node["requires_purchase"] = True
                            has_purchase_reward = True
                        wanted_drops.append(drop_node)

                if len(wanted_drops) > 0:
                    node = {
                        "id": campaign.id,
                        "name": campaign.name,
                        "url": campaign.campaign_url,
                        "drops": wanted_drops,
                    }
                    linked = getattr(campaign, "linked", None)
                    has_badge_or_emote = getattr(campaign, "has_badge_or_emote", None)
                    if for_display and linked is False and not has_badge_or_emote:
                        node["needs_account_link"] = True
                        node["link_url"] = getattr(campaign, "link_url", "")
                    if for_display and has_purchase_reward:
                        slug = getattr(campaign.game, "slug", "")
                        node["channels_url"] = (
                            f"https://www.twitch.tv/directory/category/{slug}?filter=drops"
                        )
                    wanted_campaigns.append(node)

            if len(wanted_campaigns) > 0:
                wanted_games.append(
                    {
                        "game_id": game_obj.id if game_obj else None,
                        "game_name": game_name,
                        "game_icon": game_obj.box_art_url if game_obj else None,
                        "game_obj": game_obj,
                        "campaigns": wanted_campaigns,
                    }
                )

        return wanted_games

    def get_wanted_game_tree(
        self, settings: Settings, campaigns: list[DropsCampaign]
    ) -> list[dict]:
        return [
            {**game, "game_obj": None}
            for game in self._get_wanted_game_tree(settings, campaigns, for_display=True)
        ]

    def get_wanted_games(self, settings: Settings, campaigns: list[DropsCampaign]) -> list[Game]:
        return [game["game_obj"] for game in self._get_wanted_game_tree(settings, campaigns)]
