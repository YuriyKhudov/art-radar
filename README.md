# Art Radar

Хаб для художника, который следит за американским арт-рынком: новые видео YouTube-каналов о выставках и ярмарках, новости арт-изданий, календарь ярмарок США и статистика размеров проданных работ.

Сайт собирается автоматически каждый час (GitHub Actions → GitHub Pages).

## Источники

- Видео — официальные RSS-ленты YouTube-каналов из `sources_seed.json`.
- Новости — RSS-ленты арт-изданий из `sources_seed.json`; статьи открываются на сайтах изданий.
- Статистика проданных работ — Kangsan Lee et al., *Buying a Work of Art or an Artist? Exploring the Impact of Visual and Social Features on Art Prices*, Springer Nature figshare, 2024, лицензия [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): <https://springernature.figshare.com/articles/dataset/24746268>. Из набора взяты живопись и графика, проданные на аукционах в долларах США; размеры переведены в сантиметры, работы сгруппированы по форматам.

## Локальный запуск

```
python -m radar.app
```
