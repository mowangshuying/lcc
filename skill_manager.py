from pathlib import Path
import yaml

class SkillManager:
    def __init__(self, skillsDir: Path):
        self.skillsDir = skillsDir
        self.skills: dict[str, dict[str, str]] = {}
        self.scan()
        
    @staticmethod
    def parse_frontmatter(text: str) -> tuple[dict, str]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return {}, text

        closing_index = next(
            (index for index, line in enumerate(lines[1:], start=1)
             if line.rstrip("\r\n") == "---"),
            None,
        )
        if closing_index is None:
            return {}, text

        frontmatter = "".join(lines[1:closing_index])
        body = "".join(lines[closing_index + 1:]).strip()
        try:
            metadata = yaml.safe_load(frontmatter) or {}
        except yaml.YAMLError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return metadata, body
    
    def scan(self):
        self.skills.clear()
        if not self.skillsDir.exists():
            return

        skillsRoot = self.skillsDir.resolve()
        for manifest in sorted(self.skillsDir.glob("*/SKILL.md")):
            if (not manifest.is_file() or not manifest.resolve().is_relative_to(skillsRoot)):
                continue
            
            content = manifest.read_text(encoding="utf-8")
            metadata, body = self.parse_frontmatter(content)

            ### Name;
            name = ""             
            rawName = metadata.get("name")
            if isinstance(rawName, str):
                name = rawName.strip()
                
            if not name:
                name = manifest.parent.name
            
            ### Description;
            description = ""
            rawDescription = metadata.get("description")
            if isinstance(rawDescription, str):
                description = rawDescription.strip()
                
            if not description:
                description = body.split("\n", 1)[0]
            
            #### 数据清洗   
            #### lstrip 去除左侧所有空格及#;
            #### split 会按连续空白字符拆分字符串;
            #### eg: "#  Hello World  " => "Hello World"  
            description = " ".join(str(description).lstrip("# ").split())
            self.skills[name] = {
                "name": name,
                "description": description,
                "content": content,
            }
            
            
    ### 列出技能摘要
    def catalog(self) -> str:
        if not self.skills:
            return "(no skills found)"
        
        skills = []
        for skill in self.skills.values():
            s =  f"- {skill['name']}: {skill['description']}"
            skills.append(s)
        return "\n".join(skills)
    
    ### 根据名字进行加载skill;
    def load(self, name: str) -> str:
        skill = self.skills.get(name)
        if skill:
            return skill["content"]
        return f"Error: Unknown skill '{name}'"