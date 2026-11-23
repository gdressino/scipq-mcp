package com.example.shop;

public class CountBadge extends BaseBadge {
    @Override
    protected void refresh() {
        Pages.count();
    }
}
