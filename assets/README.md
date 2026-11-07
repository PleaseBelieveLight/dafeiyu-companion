# 大肥鱼角色素材

本目录的大肥鱼人物形象由 PleaseBelieveLight 自行设计。使用或再分发角色素材时，请遵循素材权利人的授权。

[基础图](character/base.png)用于定义大肥鱼的外观，也是自拍的默认参考。场景图按动作或表情命名。
新增或替换参考图时，统一导出为 1024 × 1024 的 PNG。

| 场景 | 参考图 |
| --- | --- |
| 吃饭、用餐或吃零食 | [eating.png](character/scenes/eating.png) |
| 困惑、疑惑或挠头 | [confused.png](character/scenes/confused.png) |
| 惊讶或震惊 | [surprised.png](character/scenes/surprised.png) |
| 招手、打招呼或微笑 | [waving.png](character/scenes/waving.png) |
| 睡觉、睡前或晚安 | [sleeping.png](character/scenes/sleeping.png) |
| 跳舞、转圈、摆姿势或伸懒腰 | [dancing.png](character/scenes/dancing.png) |
| 窗边坐姿、看书、看手机或发呆 | [sitting.png](character/scenes/sitting.png) |

动态与坐姿两张图通过内置 imagegen，以基础图和当前人格设定为参考生成。
动态姿势表现开心时藏不住的尾巴与一点小得意，坐姿表现清醒、放松的日常陪伴。

自拍会根据本次动作描述选择一张角色参考图。未匹配场景或缺少对应图片时使用基础图，基础图缺失时使用插件中配置的参考图。
服装、画风、动作和构图以本次要求为准。用户附图也会参与生图，其他人设使用各自配置的参考图。

首次安装和[部署更新](../scripts/README.md)都会把图片复制到运行目录的万象画卷插件数据目录。
已有安装在源码更新后运行 `scripts/维护/部署更新.cmd` 即可使用新图。
